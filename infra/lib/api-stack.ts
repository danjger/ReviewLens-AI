/**
 * ApiStack – the synchronous HTTP tier and the event/queue backbone
 * (platform-foundation task 6.3, Lambda compute mode).
 *
 * Provides:
 *   - EventBridge custom bus (`reviewlens`) that every Service publishes domain
 *     events to (`dataset.status.changed`, `check.updated`, `chat.exchange.saved`).
 *   - SQS queues, each with a dead-letter queue and redrive maxReceiveCount 3
 *     (matching app.consumer.MAX_RECEIVE_COUNT):
 *       - `check-queue`         (standard) – one message per URL check item
 *       - `processing-queue.fifo` (FIFO)   – one message per dataset at a time
 *       - `push-queue`          (standard) – real-time push fan-out
 *     Visibility timeout is 6× the owning Lambda timeout per the design's
 *     Error Handling section.
 *   - API service Lambda: the `backend` container image behind an API Gateway
 *     HTTP API (default `$default` proxy route). No VPC — the database is
 *     reached through the RDS Data API over HTTPS (Requirement 7.4).
 *   - Chat service Lambda: the same `backend` image running `app.chat`, exposed
 *     through a Lambda Function URL in RESPONSE_STREAM invoke mode. CloudFront
 *     fronts it and the origin guard protects it, so the Function URL auth type
 *     is NONE.
 *
 * No Service runs inside a VPC and there is no NAT gateway (Requirement 7.4).
 *
 * Requirements: 1.1, 3.2, 7.1, 7.4
 *
 * ──────────────────────────────────────────────────────────────────────────
 * IMAGE ASSET: the API and chat Lambdas use DockerImageCode.fromImageAsset on
 * the backend directory. `cdk synth` only builds the image when the asset is
 * actually staged; with `--no-asset-metadata` / in environments without a
 * docker daemon the synth still succeeds (the asset is built later by CI,
 * which pushes the tested digest). CI builds and pushes the image.
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as events from "aws-cdk-lib/aws-events";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import type { DataStack } from "./data-stack";
import type { ComputeTier } from "./compute-mode";

/** Logical name of the application EventBridge bus. */
const EVENT_BUS_NAME = "reviewlens";

/** SQS redrive maxReceiveCount — matches app.consumer.MAX_RECEIVE_COUNT. */
const MAX_RECEIVE_COUNT = 3;

/** HTTP service Lambda timeout. */
const API_TIMEOUT = cdk.Duration.seconds(30);

/** Chat service Lambda timeout — streaming responses can run longer. */
const CHAT_TIMEOUT = cdk.Duration.minutes(2);

/**
 * Timeout used for the Lambdas that consume each queue (workers stack, task
 * 6.4). The queue visibility timeout must be ≥ 6× this, per the design's Error
 * Handling guidance. Defined here because the queues live in this stack.
 */
const WORKER_TIMEOUT = cdk.Duration.minutes(5);

/** Absolute path to the backend image build context (../backend). */
const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");

export interface ApiStackProps extends cdk.StackProps {
  /** The DataStack whose resources the API/chat Lambdas use. */
  readonly data: DataStack;

  /**
   * Deployment environment name injected as `ENV` (defaults to `production`).
   * Note: never set `SSRF_TEST_ALLOW_HOSTS` here — config refuses to start in
   * production with it set.
   */
  readonly envName?: string;
}

/**
 * ApiStack is the Lambda-mode {@link ComputeTier}: it provides the compute the
 * Edge stack fronts. `implements ComputeTier` makes that contract explicit, so
 * bin/app.ts can wire Edge to the Api stack or the future container-mode
 * ContainersStack interchangeably (platform-foundation task 6.5, Req 3.9).
 */
export class ApiStack extends cdk.Stack implements ComputeTier {
  /** The application EventBridge bus. */
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

  /** API service Lambda (backend image, HTTP API). */
  public readonly apiFunction: lambda.DockerImageFunction;
  /** Chat service Lambda (backend image, streaming Function URL). */
  public readonly chatFunction: lambda.DockerImageFunction;

  /** The HTTP API fronting the API Lambda. */
  public readonly httpApi: apigwv2.HttpApi;

  /**
   * Domain name of the HTTP API (host only, no scheme). Passed to the Edge
   * stack as the `/api/*` origin.
   */
  public readonly httpApiDomain: string;

  /**
   * Domain name of the chat Function URL (host only, no scheme). Passed to the
   * Edge stack as the `/api/chat/*` origin.
   */
  public readonly chatFunctionUrlDomain: string;

  /**
   * {@link ComputeTier} API origin — the host-only `/api/*` origin domain.
   * In Lambda mode this is the API Gateway HTTP API domain.
   */
  public get apiDomain(): string {
    return this.httpApiDomain;
  }

  /**
   * {@link ComputeTier} chat origin — the host-only `/api/chat/*` origin
   * domain. In Lambda mode this is the chat Function URL domain.
   */
  public get chatDomain(): string {
    return this.chatFunctionUrlDomain;
  }

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const { data } = props;
    const envName = props.envName ?? "production";

    // ------------------------------------------------------------------
    // EventBridge bus
    // ------------------------------------------------------------------
    // Custom application bus. Its name must match Settings.eventbridge_bus_name
    // injected into every Service's EVENTBRIDGE_BUS_NAME env var.
    this.eventBus = new events.EventBus(this, "EventBus", {
      eventBusName: EVENT_BUS_NAME,
    });

    // ------------------------------------------------------------------
    // SQS queues (each with a DLQ + redrive maxReceiveCount 3)
    // ------------------------------------------------------------------
    // Visibility timeout = 6× the consuming Lambda timeout (Error Handling):
    // long enough that a slow handler never has its message redelivered while
    // still in flight.
    const workerVisibility = cdk.Duration.seconds(
      WORKER_TIMEOUT.toSeconds() * 6
    );

    // check-queue (standard) + DLQ.
    this.checkDlq = new sqs.Queue(this, "CheckDlq", {
      queueName: "check-queue-dlq",
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.checkQueue = new sqs.Queue(this, "CheckQueue", {
      queueName: "check-queue",
      visibilityTimeout: workerVisibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.checkDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    // processing-queue.fifo (FIFO) + FIFO DLQ.
    //
    // The producer supplies both the message group ID (dataset ID — one
    // message per dataset in flight) and the deduplication ID
    // (`dataset_id:version`), so content-based deduplication is OFF. The DLQ of
    // a FIFO queue must itself be FIFO.
    this.processingDlq = new sqs.Queue(this, "ProcessingDlq", {
      queueName: "processing-queue-dlq.fifo",
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.processingQueue = new sqs.Queue(this, "ProcessingQueue", {
      queueName: "processing-queue.fifo",
      fifo: true,
      // Dedup ID is supplied by the producer (dataset_id:version), so do not
      // let SQS derive it from the body.
      contentBasedDeduplication: false,
      visibilityTimeout: workerVisibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.processingDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    // push-queue (standard) + DLQ.
    this.pushDlq = new sqs.Queue(this, "PushDlq", {
      queueName: "push-queue-dlq",
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    this.pushQueue = new sqs.Queue(this, "PushQueue", {
      queueName: "push-queue",
      visibilityTimeout: workerVisibility,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.pushDlq,
        maxReceiveCount: MAX_RECEIVE_COUNT,
      },
    });

    // The push-queue is the EventBridge fan-out target for the real-time
    // channel (dataset-library): a rule on the application bus routes
    // dataset.status.changed and check.updated here. The rule lives in the
    // RealtimeStack, which owns both the WebSocket API and the rule. That rule
    // imports this queue BY NAME (not as a CloudFormation token) so the SQS
    // target does not add a rule-ARN-scoped policy back here — which would make
    // this stack depend on the RealtimeStack and create a cross-stack cycle
    // (this stack already provides the bus the rule reads). To keep the
    // dependency one-directional, the queue's policy grant for EventBridge is
    // declared HERE, where the queue (and its policy) is owned. It is scoped to
    // the EventBridge service principal in this account; no rule ARN is
    // referenced, so no dependency on the RealtimeStack is introduced.
    this.pushQueue.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AllowEventBridgeFanoutToSend",
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal("events.amazonaws.com")],
        actions: ["sqs:SendMessage"],
        resources: [this.pushQueue.queueArn],
        conditions: {
          StringEquals: { "aws:SourceAccount": cdk.Stack.of(this).account },
        },
      })
    );

    // ------------------------------------------------------------------
    // Shared environment for the HTTP Lambdas
    // ------------------------------------------------------------------
    // Field names match app.core.config.Settings (read from env, case
    // insensitive). Model IDs come from config, never literals in app code —
    // we inject the Settings defaults here so they are overridable per deploy.
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

      // Origin guard shared secret ARN. core.config reads ORIGIN_VERIFY_SECRET
      // from the merged secret; point SECRETS_ARN handling at it via the
      // dedicated secret as well so both the API key and the origin secret are
      // available. We expose the ARN; the app resolves the value at startup.
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

    // ------------------------------------------------------------------
    // API service Lambda (backend image) — behind an HTTP API, NO VPC
    // ------------------------------------------------------------------
    this.apiFunction = new lambda.DockerImageFunction(this, "ApiFunction", {
      functionName: "reviewlens-api",
      // The backend image's default CMD already serves app.api via LWA. No VPC
      // is attached, so the function reaches the DB via the Data API over HTTPS
      // (Requirement 7.4) and has no NAT dependency.
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: API_TIMEOUT,
      environment: {
        ...commonEnv,
        SERVICE_NAME: "api",
        // LWA invoke mode: buffered responses for the API (non-streaming).
        AWS_LWA_INVOKE_MODE: "buffered",
      },
    });

    // ------------------------------------------------------------------
    // Chat service Lambda (backend image, app.chat) — streaming Function URL
    // ------------------------------------------------------------------
    this.chatFunction = new lambda.DockerImageFunction(this, "ChatFunction", {
      functionName: "reviewlens-chat",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        // Override the image CMD so this function serves the chat app instead
        // of the default app.api. LWA proxies to whatever listens on $PORT.
        cmd: [
          "uvicorn",
          "app.chat:app",
          "--host",
          "0.0.0.0",
          "--port",
          "8080",
        ],
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: CHAT_TIMEOUT,
      environment: {
        ...commonEnv,
        SERVICE_NAME: "chat",
        // Response streaming: LWA streams the body back through the Function URL.
        AWS_LWA_INVOKE_MODE: "response_stream",
      },
    });

    // Chat Function URL in RESPONSE_STREAM mode. Auth type NONE: CloudFront
    // fronts it and the origin guard (X-Origin-Verify) protects it.
    const chatUrl = this.chatFunction.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
    });

    // ------------------------------------------------------------------
    // Permissions
    // ------------------------------------------------------------------
    // Both HTTP Lambdas need the same core grants. The API Lambda additionally
    // enqueues check/processing work; the chat Lambda publishes events.
    for (const fn of [this.apiFunction, this.chatFunction]) {
      // Aurora via the Data API + read the DB credentials secret.
      data.cluster.grantDataApiAccess(fn);
      if (data.cluster.secret) {
        data.cluster.secret.grantRead(fn);
      }
      // S3 read/write for captures, uploads, reviews, chat objects.
      data.bucket.grantReadWrite(fn);
      // DynamoDB (rate limits, check sessions, ws connections).
      data.rateLimitsTable.grantReadWriteData(fn);
      data.checkSessionsTable.grantReadWriteData(fn);
      data.wsConnectionsTable.grantReadWriteData(fn);
      // Secrets read (Anthropic API key + origin-verify secret).
      data.anthropicSecret.grantRead(fn);
      data.originVerifySecret.grantRead(fn);
      // Publish domain events to the application bus.
      this.eventBus.grantPutEventsTo(fn);
    }

    // API Lambda produces check and processing work.
    this.checkQueue.grantSendMessages(this.apiFunction);
    this.processingQueue.grantSendMessages(this.apiFunction);

    // ------------------------------------------------------------------
    // API Gateway HTTP API fronting the API Lambda
    // ------------------------------------------------------------------
    // A single $default route proxies everything to the API Lambda; CloudFront
    // routes only `/api/*` here, and the app owns the `/api` prefix.
    const apiIntegration = new HttpLambdaIntegration(
      "ApiIntegration",
      this.apiFunction
    );
    this.httpApi = new apigwv2.HttpApi(this, "HttpApi", {
      apiName: "reviewlens-api",
      defaultIntegration: apiIntegration,
    });

    // ------------------------------------------------------------------
    // Exposed domains for the Edge stack
    // ------------------------------------------------------------------
    // HTTP API endpoint is `https://<id>.execute-api.<region>.amazonaws.com`;
    // strip the scheme so CloudFront's HttpOrigin receives a bare host.
    this.httpApiDomain = cdk.Fn.select(1, cdk.Fn.split("://", this.httpApi.apiEndpoint));

    // Function URL is `https://<id>.lambda-url.<region>.on.aws/`; strip scheme
    // and trailing slash to get the bare host for CloudFront.
    this.chatFunctionUrlDomain = cdk.Fn.select(
      2,
      cdk.Fn.split("/", chatUrl.url)
    );

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "HttpApiEndpoint", {
      value: this.httpApi.apiEndpoint,
      description: "API Gateway HTTP API endpoint (the /api/* origin)",
    });
    new cdk.CfnOutput(this, "HttpApiDomain", {
      value: this.httpApiDomain,
      description: "API Gateway HTTP API domain (host only)",
    });
    new cdk.CfnOutput(this, "ChatFunctionUrl", {
      value: chatUrl.url,
      description: "Chat service streaming Function URL",
    });
    new cdk.CfnOutput(this, "ChatFunctionUrlDomain", {
      value: this.chatFunctionUrlDomain,
      description: "Chat service Function URL domain (host only, the /api/chat/* origin)",
    });
    new cdk.CfnOutput(this, "EventBusName", {
      value: this.eventBus.eventBusName,
      description: "Application EventBridge bus name",
    });
    new cdk.CfnOutput(this, "CheckQueueUrl", { value: this.checkQueue.queueUrl });
    new cdk.CfnOutput(this, "ProcessingQueueUrl", {
      value: this.processingQueue.queueUrl,
    });
    new cdk.CfnOutput(this, "PushQueueUrl", { value: this.pushQueue.queueUrl });
  }
}
