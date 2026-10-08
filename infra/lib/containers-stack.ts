/**
 * ContainersStack – the optional ECS Fargate compute tier (platform-foundation
 * task 6.10, container compute mode).
 *
 * Requirement 3.9: a container mode can be added per Service WITHOUT changing
 * application code. The SAME `backend` and `workers` images that run on Lambda
 * run here as long-running containers; the only difference is the command the
 * container runs. HTTP Services run `uvicorn app.{api,chat}:app`; queue
 * consumers run `python -m app.consumer --queue <name>`; the sweeper runs
 * `python -m app.jobs.sweep`. No application code changes.
 *
 * In container mode, bin/app.ts instantiates THIS stack in place of the
 * ApiStack + WorkersStack. Because ApiStack is not created in this mode, the
 * EventBridge bus and the SQS queues (which are needed regardless of compute
 * mode) are duplicated here with the SAME names/config as ApiStack, keeping
 * container mode fully self-contained. (A shared MessagingStack refactor is out
 * of scope for this task; the queue/bus definitions mirror ApiStack exactly.)
 *
 * Networking / egress (Requirement 7.4 — no NAT gateway):
 *   The DataStack VPC is private-isolated with 0 NAT gateways, so it cannot
 *   give Fargate tasks internet egress (needed to reach the Anthropic Claude
 *   API and review sites). Rather than add a NAT gateway, this stack creates
 *   its OWN VPC with PUBLIC subnets only and runs every Fargate task with
 *   `assignPublicIp: true`. A task in a public subnet with a public IP reaches
 *   the internet through the VPC's internet gateway — no NAT gateway. The
 *   database is still reached over HTTPS through the RDS Data API (no VPC
 *   attachment to the Aurora VPC is required: the Data API is a public AWS
 *   endpoint), so "no NAT gateway" (Requirement 7.4) holds in container mode.
 *
 * ComputeTier: this stack implements {@link ComputeTier} by exposing
 * `apiDomain` and `chatDomain`. Both API and chat run behind ONE internet-
 * facing ALB with path-based routing (`/api/chat/*` → chat target group,
 * everything else → api target group), so both domains are the same ALB DNS
 * name; CloudFront routes `/api/chat/*` and `/api/*` to it. The Edge stack
 * wiring in bin/app.ts needs no change.
 *
 * Requirements: 3.2, 3.9 (also 7.4 — no NAT gateway — carried through).
 *
 * ──────────────────────────────────────────────────────────────────────────
 * IMAGE / COMMAND ASSUMPTION
 *
 * Container mode uses the SAME image assets as Lambda mode (same build context
 * `../backend`, `Dockerfile` for the backend image and `Dockerfile.workers` for
 * the workers image). Each service overrides the container COMMAND to select
 * what the image runs — the images support being launched directly (uvicorn /
 * `python -m ...`) in addition to the Lambda entrypoints. This is the standard
 * "same image, both compute modes" contract from the design's Technology
 * choices ("the switch is in infrastructure only").
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as events from "aws-cdk-lib/aws-events";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as schedulerTargets from "aws-cdk-lib/aws-scheduler-targets";
import type { DataStack } from "./data-stack";
import type { ComputeTier } from "./compute-mode";

/** Logical name of the application EventBridge bus (mirrors ApiStack). */
const EVENT_BUS_NAME = "reviewlens";

/** SQS redrive maxReceiveCount — matches app.consumer.MAX_RECEIVE_COUNT. */
const MAX_RECEIVE_COUNT = 3;

/**
 * Worker handling budget mirrored from ApiStack/WorkersStack (5 min). The
 * queues' visibility timeout is 6× this so a slow handler never has its message
 * redelivered while still in flight.
 */
const WORKER_TIMEOUT = cdk.Duration.minutes(5);

/** Absolute path to the backend/workers image build context (../backend). */
const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");

/** Container port the HTTP apps (uvicorn) listen on. */
const APP_PORT = 8080;

/** ALB listener port (HTTP; TLS is terminated at CloudFront). */
const ALB_PORT = 80;

export interface ContainersStackProps extends cdk.StackProps {
  /** The DataStack whose resources the services use (DB, bucket, tables, secrets). */
  readonly data: DataStack;

  /**
   * Deployment environment name injected as `ENV` (defaults to `production`).
   * Never set `SSRF_TEST_ALLOW_HOSTS` here — config refuses to start in
   * production with it set.
   */
  readonly envName?: string;
}

/**
 * ContainersStack is the container-mode {@link ComputeTier}: it provides the
 * compute the Edge stack fronts, exposed through an internet-facing ALB. Both
 * `apiDomain` and `chatDomain` resolve to the ALB DNS name (one ALB, path-based
 * routing), so bin/app.ts wires Edge to this stack exactly as it wires the
 * Lambda-mode ApiStack (platform-foundation task 6.5, Requirement 3.9).
 */
export class ContainersStack extends cdk.Stack implements ComputeTier {
  /** The application EventBridge bus (duplicated from ApiStack for self-containment). */
  public readonly eventBus: events.EventBus;

  /** Standard check queue (one message per URL check item). */
  public readonly checkQueue: sqs.Queue;
  /** FIFO processing queue (one message per dataset, group = dataset ID). */
  public readonly processingQueue: sqs.Queue;
  /** Standard push queue (real-time fan-out). */
  public readonly pushQueue: sqs.Queue;

  /** Dead-letter queues. */
  public readonly checkDlq: sqs.Queue;
  public readonly processingDlq: sqs.Queue;
  public readonly pushDlq: sqs.Queue;

  /** The ECS cluster hosting every Fargate service/task. */
  public readonly cluster: ecs.Cluster;

  /** Internet-facing ALB fronting the API and chat services. */
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;

  /** The ALB DNS name — the origin CloudFront fronts for both /api/* and /api/chat/*. */
  public readonly albDomain: string;

  /** {@link ComputeTier} API origin — the ALB DNS name (path-routed). */
  public get apiDomain(): string {
    return this.albDomain;
  }

  /**
   * {@link ComputeTier} chat origin — the SAME ALB DNS name. CloudFront routes
   * `/api/chat/*` here; the ALB's path rule forwards it to the chat service.
   */
  public get chatDomain(): string {
    return this.albDomain;
  }

  constructor(scope: Construct, id: string, props: ContainersStackProps) {
    super(scope, id, props);

    const { data } = props;
    const envName = props.envName ?? "production";

    // ------------------------------------------------------------------
    // EventBridge bus (duplicated from ApiStack — self-contained container mode)
    // ------------------------------------------------------------------
    this.eventBus = new events.EventBus(this, "EventBus", {
      eventBusName: EVENT_BUS_NAME,
    });

    // ------------------------------------------------------------------
    // SQS queues (duplicated from ApiStack — same names/config)
    // ------------------------------------------------------------------
    const visibility = cdk.Duration.seconds(WORKER_TIMEOUT.toSeconds() * 6);

    this.checkDlq = new sqs.Queue(this, "CheckDlq", {
      queueName: "check-queue-dlq",
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.checkQueue = new sqs.Queue(this, "CheckQueue", {
      queueName: "check-queue",
      visibilityTimeout: visibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.checkDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    this.processingDlq = new sqs.Queue(this, "ProcessingDlq", {
      queueName: "processing-queue-dlq.fifo",
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.processingQueue = new sqs.Queue(this, "ProcessingQueue", {
      queueName: "processing-queue.fifo",
      fifo: true,
      contentBasedDeduplication: false,
      visibilityTimeout: visibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.processingDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    this.pushDlq = new sqs.Queue(this, "PushDlq", {
      queueName: "push-queue-dlq",
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.pushQueue = new sqs.Queue(this, "PushQueue", {
      queueName: "push-queue",
      visibilityTimeout: visibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.pushDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    // ------------------------------------------------------------------
    // Networking — OWN VPC, PUBLIC subnets only, NO NAT gateway (Req 7.4)
    // ------------------------------------------------------------------
    // Fargate tasks need internet egress (Claude + review sites). A public
    // subnet + assignPublicIp reaches the internet via the internet gateway
    // with NO NAT gateway, honouring Requirement 7.4. The DB is reached over
    // HTTPS through the RDS Data API (a public AWS endpoint), so this VPC does
    // not need to peer with the Aurora VPC.
    const vpc = new ec2.Vpc(this, "ContainersVpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
      ],
    });

    // ------------------------------------------------------------------
    // ECS Fargate cluster
    // ------------------------------------------------------------------
    this.cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: "reviewlens",
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // Tasks run in the public subnets with a public IP (egress, no NAT).
    const taskSubnets: ec2.SubnetSelection = {
      subnetType: ec2.SubnetType.PUBLIC,
    };

    // ------------------------------------------------------------------
    // Shared environment — matches app.core.config.Settings field names.
    // Identical to ApiStack/WorkersStack commonEnv so a container resolves the
    // same config as the Lambda tier. SERVICE_NAME is set per service below.
    // ------------------------------------------------------------------
    const commonEnv: Record<string, string> = {
      ENV: envName,

      // Storage
      S3_BUCKET: data.bucket.bucketName,

      // Database (Aurora Data API — reached over HTTPS, no VPC attachment)
      DB_RESOURCE_ARN: data.clusterArn,
      DB_SECRET_ARN: data.dbSecretArn,
      DB_DATABASE_NAME: data.databaseName,

      // Secrets loaded at startup (core.config merges the secret JSON into env)
      SECRETS_ARN: data.anthropicSecret.secretArn,
      ORIGIN_VERIFY_SECRET_ARN: data.originVerifySecret.secretArn,

      // Queues
      CHECK_QUEUE_URL: this.checkQueue.queueUrl,
      PROCESSING_QUEUE_URL: this.processingQueue.queueUrl,
      PUSH_QUEUE_URL: this.pushQueue.queueUrl,

      // EventBridge
      EVENTBRIDGE_BUS_NAME: this.eventBus.eventBusName,

      // DynamoDB tables
      DYNAMODB_RATE_LIMIT_TABLE: data.rateLimitsTable.tableName,
      CHECK_SESSIONS_TABLE: data.checkSessionsTable.tableName,
      WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
    };

    // Container images (SAME assets as Lambda mode; command overridden below).
    const backendImage = ecs.ContainerImage.fromAsset(BACKEND_DIR);
    const workersImage = ecs.ContainerImage.fromAsset(BACKEND_DIR, {
      file: "Dockerfile.workers",
    });

    // One shared log group keeps CloudWatch tidy; each container gets its own
    // stream prefix (the SERVICE_NAME).
    const logGroup = new logs.LogGroup(this, "ServiceLogs", {
      logGroupName: "/reviewlens/containers",
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    /** Grant a task role everything a worker/HTTP service needs on the data tier. */
    const grantCoreData = (role: cdk.aws_iam.IGrantable): void => {
      data.cluster.grantDataApiAccess(role);
      if (data.cluster.secret) {
        data.cluster.secret.grantRead(role);
      }
      data.anthropicSecret.grantRead(role);
      data.originVerifySecret.grantRead(role);
    };

    // ==================================================================
    // Internet-facing ALB (one ALB fronts both API and chat)
    // ==================================================================
    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc,
      internetFacing: true,
      vpcSubnets: taskSubnets,
    });
    const listener = this.loadBalancer.addListener("HttpListener", {
      port: ALB_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      // Default action: anything not matched by a rule 404s. The API rule below
      // becomes the catch-all for /api/* and the SPA; chat is a priority rule.
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: "text/plain",
        messageBody: "Not found",
      }),
    });
    this.albDomain = this.loadBalancer.loadBalancerDnsName;

    // ------------------------------------------------------------------
    // API Fargate service (backend image, uvicorn app.api:app)
    // ------------------------------------------------------------------
    const apiTaskDef = new ecs.FargateTaskDefinition(this, "ApiTaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    apiTaskDef.addContainer("api", {
      image: backendImage,
      command: ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", String(APP_PORT)],
      environment: { ...commonEnv, SERVICE_NAME: "api" },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "api" }),
      portMappings: [{ containerPort: APP_PORT }],
    });
    const apiService = new ecs.FargateService(this, "ApiService", {
      cluster: this.cluster,
      serviceName: "reviewlens-api",
      taskDefinition: apiTaskDef,
      desiredCount: 2,
      assignPublicIp: true,
      vpcSubnets: taskSubnets,
      minHealthyPercent: 50,
    });
    grantCoreData(apiService.taskDefinition.taskRole);
    data.bucket.grantReadWrite(apiService.taskDefinition.taskRole);
    data.rateLimitsTable.grantReadWriteData(apiService.taskDefinition.taskRole);
    data.checkSessionsTable.grantReadWriteData(apiService.taskDefinition.taskRole);
    data.wsConnectionsTable.grantReadWriteData(apiService.taskDefinition.taskRole);
    this.eventBus.grantPutEventsTo(apiService.taskDefinition.taskRole);
    this.checkQueue.grantSendMessages(apiService.taskDefinition.taskRole);
    this.processingQueue.grantSendMessages(apiService.taskDefinition.taskRole);

    const apiTargetGroup = new elbv2.ApplicationTargetGroup(this, "ApiTg", {
      vpc,
      port: APP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [apiService],
      healthCheck: {
        path: "/readyz",
        healthyHttpCodes: "200",
        interval: cdk.Duration.seconds(30),
      },
    });
    // Default rule for the listener: everything not /api/chat/* goes to the API.
    listener.addAction("ApiDefault", {
      action: elbv2.ListenerAction.forward([apiTargetGroup]),
    });

    // CPU-based autoscaling for the API HTTP service.
    const apiScaling = apiService.autoScaleTaskCount({
      minCapacity: 2,
      maxCapacity: 10,
    });
    apiScaling.scaleOnCpuUtilization("ApiCpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ------------------------------------------------------------------
    // Chat Fargate service (backend image, uvicorn app.chat:app)
    // ------------------------------------------------------------------
    const chatTaskDef = new ecs.FargateTaskDefinition(this, "ChatTaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    chatTaskDef.addContainer("chat", {
      image: backendImage,
      command: ["uvicorn", "app.chat:app", "--host", "0.0.0.0", "--port", String(APP_PORT)],
      environment: { ...commonEnv, SERVICE_NAME: "chat" },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "chat" }),
      portMappings: [{ containerPort: APP_PORT }],
    });
    const chatService = new ecs.FargateService(this, "ChatService", {
      cluster: this.cluster,
      serviceName: "reviewlens-chat",
      taskDefinition: chatTaskDef,
      desiredCount: 2,
      assignPublicIp: true,
      vpcSubnets: taskSubnets,
      minHealthyPercent: 50,
    });
    grantCoreData(chatService.taskDefinition.taskRole);
    data.bucket.grantReadWrite(chatService.taskDefinition.taskRole);
    data.rateLimitsTable.grantReadWriteData(chatService.taskDefinition.taskRole);
    data.checkSessionsTable.grantReadWriteData(chatService.taskDefinition.taskRole);
    data.wsConnectionsTable.grantReadWriteData(chatService.taskDefinition.taskRole);
    this.eventBus.grantPutEventsTo(chatService.taskDefinition.taskRole);

    const chatTargetGroup = new elbv2.ApplicationTargetGroup(this, "ChatTg", {
      vpc,
      port: APP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [chatService],
      healthCheck: {
        path: "/readyz",
        healthyHttpCodes: "200",
        interval: cdk.Duration.seconds(30),
      },
    });
    // Priority rule: /api/chat/* routes to the chat service (streaming is fine
    // over the ALB). CloudFront's /api/chat/* behavior forwards here.
    listener.addAction("ChatRoute", {
      priority: 10,
      conditions: [elbv2.ListenerCondition.pathPatterns(["/api/chat/*"])],
      action: elbv2.ListenerAction.forward([chatTargetGroup]),
    });

    const chatScaling = chatService.autoScaleTaskCount({
      minCapacity: 2,
      maxCapacity: 10,
    });
    chatScaling.scaleOnCpuUtilization("ChatCpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ------------------------------------------------------------------
    // Job-worker Fargate services (WORKERS image), autoscaled on queue depth
    // ------------------------------------------------------------------
    // One service per queue: a check worker (`--queue check`) and a processing
    // worker (`--queue processing`). Both run the long-poll loop
    // (app.consumer.run_poller), so no ALB/target group — they pull work.
    const makeWorker = (
      scopeName: string,
      serviceName: string,
      queueArg: "check" | "processing",
      queue: sqs.Queue
    ): { service: ecs.FargateService } => {
      const taskDef = new ecs.FargateTaskDefinition(this, `${scopeName}TaskDef`, {
        // Chromium (Playwright) is memory-hungry; mirror the Lambda worker size.
        cpu: 1024,
        memoryLimitMiB: 4096,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.ARM64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
      });
      taskDef.addContainer(serviceName, {
        image: workersImage,
        command: ["python", "-m", "app.consumer", "--queue", queueArg],
        environment: { ...commonEnv, SERVICE_NAME: `${queueArg}-worker` },
        logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: queueArg }),
      });
      const service = new ecs.FargateService(this, scopeName, {
        cluster: this.cluster,
        serviceName,
        taskDefinition: taskDef,
        desiredCount: 1,
        assignPublicIp: true,
        vpcSubnets: taskSubnets,
        minHealthyPercent: 0,
        // Give in-flight work time to finish on SIGTERM (graceful shutdown).
        // The poller stops taking new messages and finishes the current one.
      });

      // Grants: every worker reaches the data tier, reads/writes the bucket and
      // tables, publishes events, and may enqueue downstream work. The worker
      // ALSO consumes its own queue, so grant consume on it.
      grantCoreData(service.taskDefinition.taskRole);
      data.bucket.grantReadWrite(service.taskDefinition.taskRole);
      data.rateLimitsTable.grantReadWriteData(service.taskDefinition.taskRole);
      data.checkSessionsTable.grantReadWriteData(service.taskDefinition.taskRole);
      data.wsConnectionsTable.grantReadWriteData(service.taskDefinition.taskRole);
      this.eventBus.grantPutEventsTo(service.taskDefinition.taskRole);
      queue.grantConsumeMessages(service.taskDefinition.taskRole);
      this.processingQueue.grantSendMessages(service.taskDefinition.taskRole);
      this.pushQueue.grantSendMessages(service.taskDefinition.taskRole);

      // Queue-depth autoscaling: scale on ApproximateNumberOfMessagesVisible.
      // scaleOnMetric steps the task count up as the backlog grows and back to
      // the minimum when the queue drains.
      const scaling = service.autoScaleTaskCount({
        minCapacity: 0,
        maxCapacity: 20,
      });
      scaling.scaleOnMetric(`${scopeName}QueueDepthScaling`, {
        metric: queue.metricApproximateNumberOfMessagesVisible({
          period: cdk.Duration.minutes(1),
          statistic: cloudwatch.Stats.MAXIMUM,
        }),
        adjustmentType:
          cdk.aws_applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        cooldown: cdk.Duration.seconds(60),
        scalingSteps: [
          { upper: 0, change: -1 },
          { lower: 1, change: +1 },
          { lower: 20, change: +2 },
          { lower: 100, change: +4 },
        ],
      });

      return { service };
    };

    makeWorker("CheckWorker", "reviewlens-check-worker", "check", this.checkQueue);
    makeWorker(
      "ProcessingWorker",
      "reviewlens-processing-worker",
      "processing",
      this.processingQueue
    );

    // ------------------------------------------------------------------
    // Push consumer Fargate service (BACKEND image, `--queue push`)
    // ------------------------------------------------------------------
    const pushTaskDef = new ecs.FargateTaskDefinition(this, "PushTaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    pushTaskDef.addContainer("push", {
      image: backendImage,
      command: ["python", "-m", "app.consumer", "--queue", "push"],
      environment: { ...commonEnv, SERVICE_NAME: "push-consumer" },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "push" }),
    });
    const pushService = new ecs.FargateService(this, "PushConsumer", {
      cluster: this.cluster,
      serviceName: "reviewlens-push-consumer",
      taskDefinition: pushTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: taskSubnets,
      minHealthyPercent: 0,
    });
    grantCoreData(pushService.taskDefinition.taskRole);
    data.wsConnectionsTable.grantReadWriteData(pushService.taskDefinition.taskRole);
    this.pushQueue.grantConsumeMessages(pushService.taskDefinition.taskRole);
    // Manage WebSocket connections (scoped to the WebSocket API once RealtimeStack lands).
    pushService.taskDefinition.taskRole.addToPrincipalPolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ["execute-api:ManageConnections"],
        resources: ["arn:aws:execute-api:*:*:*/*/*/@connections/*"],
      })
    );
    // Push volume tracks real-time events; scale on push-queue depth.
    const pushScaling = pushService.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: 10,
    });
    pushScaling.scaleOnMetric("PushQueueDepthScaling", {
      metric: this.pushQueue.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: cloudwatch.Stats.MAXIMUM,
      }),
      adjustmentType:
        cdk.aws_applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: cdk.Duration.seconds(60),
      scalingSteps: [
        { upper: 0, change: 0 },
        { lower: 1, change: +1 },
        { lower: 50, change: +2 },
      ],
    });

    // ------------------------------------------------------------------
    // Sweeper — ECS scheduled task (RunTask every 5 minutes)
    // ------------------------------------------------------------------
    // Not a long-running service: an EventBridge Scheduler fires a Fargate
    // RunTask every 5 minutes that runs `python -m app.jobs.sweep` once and
    // exits. Safe to overlap (claims rows SKIP LOCKED), so no concurrency lock
    // is needed.
    const sweeperTaskDef = new ecs.FargateTaskDefinition(this, "SweeperTaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    sweeperTaskDef.addContainer("sweeper", {
      image: backendImage,
      command: ["python", "-m", "app.jobs.sweep"],
      environment: {
        ...commonEnv,
        SERVICE_NAME: "sweeper",
        SWEEP_REQUESTED_AFTER_MIN: "5",
        SWEEP_PROCESSING_STALE_MIN: "20",
      },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "sweeper" }),
    });
    grantCoreData(sweeperTaskDef.taskRole);
    this.processingQueue.grantSendMessages(sweeperTaskDef.taskRole);
    this.eventBus.grantPutEventsTo(sweeperTaskDef.taskRole);

    // EventBridge Scheduler → ECS RunTask, every 5 minutes, in the public
    // subnets with a public IP (egress for the Data API / events, no NAT).
    new scheduler.Schedule(this, "SweeperSchedule", {
      scheduleName: "reviewlens-sweeper",
      schedule: scheduler.ScheduleExpression.rate(cdk.Duration.minutes(5)),
      target: new schedulerTargets.EcsRunFargateTask(this.cluster, {
        taskDefinition: sweeperTaskDef,
        assignPublicIp: true,
        vpcSubnets: taskSubnets,
      }),
      description: "Runs the ReviewLens sweeper every 5 minutes (ECS Fargate RunTask).",
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "AlbDomain", {
      value: this.albDomain,
      description: "ALB DNS name — the /api/* and /api/chat/* CloudFront origin",
    });
    new cdk.CfnOutput(this, "ClusterName", {
      value: this.cluster.clusterName,
      description: "ECS Fargate cluster name",
    });
    new cdk.CfnOutput(this, "EventBusName", {
      value: this.eventBus.eventBusName,
      description: "Application EventBridge bus name (container mode)",
    });
    new cdk.CfnOutput(this, "CheckQueueUrl", { value: this.checkQueue.queueUrl });
    new cdk.CfnOutput(this, "ProcessingQueueUrl", {
      value: this.processingQueue.queueUrl,
    });
    new cdk.CfnOutput(this, "PushQueueUrl", { value: this.pushQueue.queueUrl });
  }
}
