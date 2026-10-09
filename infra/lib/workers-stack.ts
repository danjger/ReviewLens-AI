/**
 * WorkersStack – the asynchronous background tier (platform-foundation task
 * 6.4, Lambda compute mode).
 *
 * Everything here is a queue consumer or a scheduled job. All of it reaches the
 * database through the RDS Data API over HTTPS, so NO function joins a VPC and
 * there is no NAT gateway (Requirement 7.4).
 *
 * Provides:
 *   1. Job-worker Lambdas (the `workers` image: backend + Playwright/Chromium),
 *      one function per consuming queue — a check-queue worker and a
 *      processing-queue (FIFO) worker. Each has an SQS event-source mapping
 *      with partial batch responses (`reportBatchItemFailures: true`), matching
 *      `app.consumer.lambda_entry`, which returns `batchItemFailures`.
 *   2. Push consumer Lambda (the `backend` image): consumes `push-queue` and
 *      delivers to the WebSocket API (RealtimeStack lands later; see the TODO).
 *   3. DLQ consumer Lambda (the `backend` image): a backstop that consumes the
 *      three dead-letter queues and marks affected datasets `failed`.
 *   4. Sweeper Lambda (the `backend` image): invoked by EventBridge Scheduler
 *      every 5 minutes. Safe to run concurrently (claims rows SKIP LOCKED).
 *
 * The queues, DLQs, and EventBridge bus are owned by the ApiStack; this stack
 * consumes them. WORKER_TIMEOUT (5 min) matches the ApiStack constant that sets
 * the queue visibility timeout to 6× it.
 *
 * Requirements: 3.4, 3.6 (also 7.4 — no VPC/NAT — carried through from 6.3).
 *
 * ──────────────────────────────────────────────────────────────────────────
 * HANDLER / CMD ASSUMPTION
 *
 * The backend and workers images are built around the AWS Lambda Web Adapter
 * (`AWS_LAMBDA_EXEC_WRAPPER`), which is for HTTP Lambdas. The queue consumers
 * are NOT HTTP: in Lambda mode they are invoked by an SQS event-source mapping
 * and must run the Python handler `app.consumer.lambda_entry` (aliased
 * `lambda_handler`). For a container-image function the handler is supplied as
 * the image CMD, so each event function below overrides CMD to
 * `["app.consumer.lambda_entry"]` and the sweeper to
 * `["app.jobs.sweep.lambda_entry"]`.
 *
 * Running a CMD of that form requires the AWS Lambda Runtime Interface Client
 * (awslambdaric) as the image entrypoint rather than the LWA exec wrapper. The
 * current Dockerfiles install the LWA wrapper but not the RIC, so these
 * functions assume the image is (or will be) built to support the RIC
 * entrypoint for event-driven invocations. Making the image serve BOTH an HTTP
 * app (LWA) and the RIC handler is an image concern (task 2.1 / CI build), out
 * of scope for this infrastructure task. The CMD override is the correct and
 * stable contract regardless of how the image wires its entrypoint.
 *
 * `app.jobs.sweep` is currently a stub; its `lambda_entry` is implemented in
 * the `review-analysis` spec. The schedule and wiring are defined here so the
 * sweeper runs the moment that handler exists.
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as schedulerTargets from "aws-cdk-lib/aws-scheduler-targets";
import type { DataStack } from "./data-stack";
import type { ApiStack } from "./api-stack";
import type { RealtimeStack } from "./realtime-stack";

/** Absolute path to the backend image build context (../backend). */
const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");

/**
 * Worker Lambda timeout. Mirrors ApiStack.WORKER_TIMEOUT (5 min); the queues'
 * visibility timeout is 6× this. Kept in sync by convention.
 */
const WORKER_TIMEOUT = cdk.Duration.minutes(5);

/** Memory for the job workers — Chromium (Playwright) is memory-hungry. */
const WORKER_MEMORY_MB = 3008;

/** Memory for the lightweight backend-image consumers (push, DLQ, sweeper). */
const LIGHT_MEMORY_MB = 1024;

/** The Lambda handler for SQS-consuming functions (see HANDLER ASSUMPTION). */
const CONSUMER_HANDLER_CMD = ["app.consumer.lambda_entry"];

/** The Lambda handler for the sweeper (implemented in review-analysis). */
const SWEEPER_HANDLER_CMD = ["app.jobs.sweep.lambda_entry"];

// The backend/workers container images bake in the AWS Lambda Web Adapter
// (AWS_LAMBDA_EXEC_WRAPPER) for the HTTP services (api/chat). Worker functions
// are NOT HTTP servers — they are native SQS/event handlers dispatched to
// `app.consumer.lambda_entry` — so they must run under the Lambda Runtime
// Interface Client (awslambdaric), not the web adapter. For each worker we
// therefore override the container ENTRYPOINT to the RIC and blank out the web
// adapter exec wrapper. (Without this the functions crash on init with
// Runtime.InvalidEntrypoint, since LWA can't dispatch a dotted-path handler.)
const RIC_ENTRYPOINT = ["/var/task/.venv/bin/python", "-m", "awslambdaric"];

/** Env overrides that disable the baked-in Lambda Web Adapter for workers. */
const DISABLE_WEB_ADAPTER: Record<string, string> = {
  AWS_LAMBDA_EXEC_WRAPPER: "",
};

/** How often the sweeper runs. Requirement 3.6 / design: every 5 minutes. */
const SWEEP_RATE = cdk.Duration.minutes(5);

/** SQS batch sizes. */
const CHECK_BATCH_SIZE = 5;
/** FIFO batch size kept at 1 so one dataset is processed at a time per group. */
const PROCESSING_BATCH_SIZE = 1;
const PUSH_BATCH_SIZE = 10;
const DLQ_BATCH_SIZE = 10;

export interface WorkersStackProps extends cdk.StackProps {
  /** The DataStack whose resources the workers use. */
  readonly data: DataStack;

  /** The ApiStack that owns the queues, DLQs, and EventBridge bus. */
  readonly api: ApiStack;

  /**
   * The RealtimeStack that owns the WebSocket API the push consumer delivers
   * to. When provided, the push consumer is given the stage callback URL as
   * `WS_API_ENDPOINT` and `execute-api:ManageConnections` scoped to that API.
   * Optional so a standalone `cdk synth` of WorkersStack still succeeds; when
   * absent the push consumer falls back to a broad ManageConnections grant and
   * no `WS_API_ENDPOINT` (it then has no WebSocket API to post to).
   */
  readonly realtime?: RealtimeStack;

  /**
   * Deployment environment name injected as `ENV` (defaults to `production`).
   * Never set `SSRF_TEST_ALLOW_HOSTS` here — config refuses to start in
   * production with it set.
   */
  readonly envName?: string;
}

export class WorkersStack extends cdk.Stack {
  /** Job-worker Lambda consuming `check-queue` (workers image). */
  public readonly checkWorker: lambda.DockerImageFunction;
  /** Job-worker Lambda consuming `processing-queue.fifo` (workers image). */
  public readonly processingWorker: lambda.DockerImageFunction;
  /** Push consumer Lambda consuming `push-queue` (backend image). */
  public readonly pushConsumer: lambda.DockerImageFunction;
  /** DLQ backstop consumer (backend image). */
  public readonly dlqConsumer: lambda.DockerImageFunction;
  /** Sweeper Lambda, invoked by EventBridge Scheduler (backend image). */
  public readonly sweeper: lambda.DockerImageFunction;

  constructor(scope: Construct, id: string, props: WorkersStackProps) {
    super(scope, id, props);

    const { data, api, realtime } = props;
    const envName = props.envName ?? "production";

    // ------------------------------------------------------------------
    // Shared environment — matches app.core.config.Settings field names.
    // Mirrors ApiStack.commonEnv so a worker resolves the same config as the
    // HTTP tier. Model IDs are left to the Settings defaults (overridable per
    // deploy); never hardcoded in app code.
    // ------------------------------------------------------------------
    const commonEnv: Record<string, string> = {
      ENV: envName,

      // Storage
      S3_BUCKET: data.bucket.bucketName,

      // Database (Aurora Data API — no VPC, Requirement 7.4)
      DB_RESOURCE_ARN: data.clusterArn,
      DB_SECRET_ARN: data.dbSecretArn,
      DB_DATABASE_NAME: data.databaseName,

      // Secrets loaded at startup (core.config merges the secret JSON into env)
      SECRETS_ARN: data.anthropicSecret.secretArn,
      ORIGIN_VERIFY_SECRET_ARN: data.originVerifySecret.secretArn,

      // Queues
      CHECK_QUEUE_URL: api.checkQueue.queueUrl,
      PROCESSING_QUEUE_URL: api.processingQueue.queueUrl,
      PUSH_QUEUE_URL: api.pushQueue.queueUrl,

      // EventBridge
      EVENTBRIDGE_BUS_NAME: api.eventBus.eventBusName,

      // DynamoDB tables
      DYNAMODB_RATE_LIMIT_TABLE: data.rateLimitsTable.tableName,
      CHECK_SESSIONS_TABLE: data.checkSessionsTable.tableName,
      WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
    };

    // ------------------------------------------------------------------
    // 1. Job-worker Lambdas (WORKERS image, NO VPC)
    // ------------------------------------------------------------------
    // Built from ../backend with Dockerfile.workers (backend + Playwright +
    // Chromium). Each overrides the image CMD to the SQS consumer handler.

    this.checkWorker = new lambda.DockerImageFunction(this, "CheckWorker", {
      functionName: "reviewlens-check-worker",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        file: "Dockerfile.workers",
        entrypoint: RIC_ENTRYPOINT,
        cmd: CONSUMER_HANDLER_CMD,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: WORKER_MEMORY_MB,
      timeout: WORKER_TIMEOUT,
      // Chromium scratch files land in /tmp (scratch only — stateless service).
      ephemeralStorageSize: cdk.Size.mebibytes(1024),
      environment: {
        ...commonEnv,
        SERVICE_NAME: "check-worker",
        ...DISABLE_WEB_ADAPTER,
      },
    });

    this.processingWorker = new lambda.DockerImageFunction(
      this,
      "ProcessingWorker",
      {
        functionName: "reviewlens-processing-worker",
        code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
          file: "Dockerfile.workers",
          entrypoint: RIC_ENTRYPOINT,
          cmd: CONSUMER_HANDLER_CMD,
        }),
        architecture: lambda.Architecture.ARM_64,
        memorySize: WORKER_MEMORY_MB,
        timeout: WORKER_TIMEOUT,
        ephemeralStorageSize: cdk.Size.mebibytes(1024),
        environment: {
          ...commonEnv,
          SERVICE_NAME: "processing-worker",
        ...DISABLE_WEB_ADAPTER,
        },
      }
    );

    // SQS event sources with partial batch responses. `reportBatchItemFailures`
    // makes the mapping honour the `batchItemFailures` list returned by
    // lambda_entry, so SQS retries only the failed messages.
    this.checkWorker.addEventSource(
      new SqsEventSource(api.checkQueue, {
        batchSize: CHECK_BATCH_SIZE,
        reportBatchItemFailures: true,
      })
    );
    this.processingWorker.addEventSource(
      new SqsEventSource(api.processingQueue, {
        // FIFO: one message per dataset group at a time.
        batchSize: PROCESSING_BATCH_SIZE,
        reportBatchItemFailures: true,
      })
    );

    // Grants for both job workers: Data API + DB secret, bucket read/write,
    // DynamoDB read/write, Anthropic secret, publish events, and enqueue
    // downstream work (a check worker may enqueue processing; workers push).
    for (const fn of [this.checkWorker, this.processingWorker]) {
      this.grantCoreData(fn, data);
      data.bucket.grantReadWrite(fn);
      data.rateLimitsTable.grantReadWriteData(fn);
      data.checkSessionsTable.grantReadWriteData(fn);
      data.wsConnectionsTable.grantReadWriteData(fn);
      api.eventBus.grantPutEventsTo(fn);
      // A check worker can promote a viable URL into processing work; workers
      // may fan out real-time push notifications.
      api.processingQueue.grantSendMessages(fn);
      api.pushQueue.grantSendMessages(fn);
    }

    // ------------------------------------------------------------------
    // 2. Push consumer Lambda (BACKEND image, NO VPC)
    // ------------------------------------------------------------------
    // Consumes push-queue and delivers to connected WebSocket clients.
    this.pushConsumer = new lambda.DockerImageFunction(this, "PushConsumer", {
      functionName: "reviewlens-push-consumer",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        file: "Dockerfile.workers",
        entrypoint: RIC_ENTRYPOINT,
        cmd: CONSUMER_HANDLER_CMD,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: LIGHT_MEMORY_MB,
      timeout: WORKER_TIMEOUT,
      environment: {
        ...commonEnv,
        SERVICE_NAME: "push-consumer",
        ...DISABLE_WEB_ADAPTER,
        // The WebSocket callback URL the handler posts to connections through.
        // Supplied by the RealtimeStack when wired; empty otherwise (the
        // handler then has no channel to deliver on — see app.handlers.push).
        ...(realtime ? { WS_API_ENDPOINT: realtime.callbackUrl } : {}),
      },
    });
    this.pushConsumer.addEventSource(
      new SqsEventSource(api.pushQueue, {
        batchSize: PUSH_BATCH_SIZE,
        reportBatchItemFailures: true,
      })
    );
    // The push consumer reads/removes WebSocket connection IDs as it fans out.
    data.wsConnectionsTable.grantReadWriteData(this.pushConsumer);
    // It also reads Data API state and the shared secret at startup.
    this.grantCoreData(this.pushConsumer, data);

    // WebSocket management: `execute-api:ManageConnections` lets the push
    // consumer postToConnection / delete stale connections.
    if (realtime) {
      // Scoped to the specific WebSocket API's @connections resource.
      realtime.webSocketApi.grantManageConnections(this.pushConsumer);
    } else {
      // Standalone synth (no RealtimeStack wired): a broad grant so the
      // function still synthesizes. bin/app.ts always passes the RealtimeStack,
      // so the scoped grant above is what deploys.
      this.pushConsumer.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["execute-api:ManageConnections"],
          resources: ["arn:aws:execute-api:*:*:*/*/*/@connections/*"],
        })
      );
    }

    // ------------------------------------------------------------------
    // 3. DLQ consumer Lambda (BACKEND image, NO VPC) — failure backstop
    // ------------------------------------------------------------------
    // Consumes all three DLQs. For each dead-lettered message it marks the
    // affected dataset `failed` (via db.status.transition), covering Lambda
    // hard timeouts where the handler never ran its final-attempt branch.
    this.dlqConsumer = new lambda.DockerImageFunction(this, "DlqConsumer", {
      functionName: "reviewlens-dlq-consumer",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        file: "Dockerfile.workers",
        entrypoint: RIC_ENTRYPOINT,
        cmd: CONSUMER_HANDLER_CMD,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: LIGHT_MEMORY_MB,
      timeout: WORKER_TIMEOUT,
      environment: {
        ...commonEnv,
        SERVICE_NAME: "dlq-consumer",
        ...DISABLE_WEB_ADAPTER,
      },
    });
    for (const dlq of [api.checkDlq, api.processingDlq, api.pushDlq]) {
      this.dlqConsumer.addEventSource(
        new SqsEventSource(dlq, {
          batchSize: DLQ_BATCH_SIZE,
          reportBatchItemFailures: true,
        })
      );
    }
    // The backstop needs Data API + DB secret to mark datasets failed and the
    // bus to publish the resulting status change.
    this.grantCoreData(this.dlqConsumer, data);
    api.eventBus.grantPutEventsTo(this.dlqConsumer);

    // ------------------------------------------------------------------
    // 4. Sweeper Lambda (BACKEND image, NO VPC) — EventBridge Scheduler
    // ------------------------------------------------------------------
    // Re-enqueues stuck `requested` datasets and fails stale `processing` ones.
    // Safe to run concurrently: it claims rows with SELECT … FOR UPDATE SKIP
    // LOCKED (Requirement 3.6).
    this.sweeper = new lambda.DockerImageFunction(this, "Sweeper", {
      functionName: "reviewlens-sweeper",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        file: "Dockerfile.workers",
        entrypoint: RIC_ENTRYPOINT,
        cmd: SWEEPER_HANDLER_CMD,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: LIGHT_MEMORY_MB,
      timeout: WORKER_TIMEOUT,
      environment: {
        ...commonEnv,
        SERVICE_NAME: "sweeper",
        ...DISABLE_WEB_ADAPTER,
        // Sweeper thresholds come from Settings defaults; expose them here so
        // they are overridable per deploy without an app change.
        SWEEP_REQUESTED_AFTER_MIN: "5",
        SWEEP_PROCESSING_STALE_MIN: "20",
      },
    });
    // Grants: Data API + DB secret (claim/transition rows), re-enqueue
    // processing work, and publish status-change events.
    this.grantCoreData(this.sweeper, data);
    api.processingQueue.grantSendMessages(this.sweeper);
    api.eventBus.grantPutEventsTo(this.sweeper);

    // EventBridge Scheduler fires the sweeper every 5 minutes. flexibleTimeWindow
    // OFF so it runs on a fixed cadence; concurrent runs are safe anyway.
    new scheduler.Schedule(this, "SweeperSchedule", {
      scheduleName: "reviewlens-sweeper",
      schedule: scheduler.ScheduleExpression.rate(SWEEP_RATE),
      target: new schedulerTargets.LambdaInvoke(this.sweeper, {}),
      description: "Runs the ReviewLens sweeper every 5 minutes.",
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "CheckWorkerName", {
      value: this.checkWorker.functionName,
    });
    new cdk.CfnOutput(this, "ProcessingWorkerName", {
      value: this.processingWorker.functionName,
    });
    new cdk.CfnOutput(this, "PushConsumerName", {
      value: this.pushConsumer.functionName,
    });
    new cdk.CfnOutput(this, "DlqConsumerName", {
      value: this.dlqConsumer.functionName,
    });
    new cdk.CfnOutput(this, "SweeperName", {
      value: this.sweeper.functionName,
    });
  }

  /**
   * Grant a function the two things every background worker needs to reach the
   * database over the Data API: the Data API itself and read access to the
   * Aurora-generated credentials secret.
   */
  private grantCoreData(
    fn: lambda.IFunction,
    data: DataStack
  ): void {
    data.cluster.grantDataApiAccess(fn);
    if (data.cluster.secret) {
      data.cluster.secret.grantRead(fn);
    }
    // EVERY worker sets SECRETS_ARN + ORIGIN_VERIFY_SECRET_ARN in commonEnv and
    // app.core.config merges both secrets into the environment at startup, so
    // every worker needs read on both — not just the AI workers. (A missing
    // grant here crashed the dlq-consumer/sweeper/push-consumer at config load
    // with AccessDenied on GetSecretValue.) Granting the AI secret to a
    // non-AI worker is harmless; it simply never calls the model.
    data.anthropicSecret.grantRead(fn);
    data.originVerifySecret.grantRead(fn);
  }
}
