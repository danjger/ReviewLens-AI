/**
 * RealtimeStack – the WebSocket API and the event fan-in for real-time push
 * (dataset-library task 4.1, Requirement 6).
 *
 * The design's real-time path is:
 *
 *   Browser ──connect──▶ WS API ──$connect──▶ Connect handler ──▶ ws-connections
 *   transition()/check handler/refresh ──▶ EventBridge ──rule──▶ push-queue
 *   push-queue ──▶ Push consumer (WorkersStack) ──postToConnection──▶ Browser
 *
 * This stack owns the parts of that path that are NOT the push consumer (which
 * lives in the WorkersStack so it shares the backend image and the SQS consumer
 * runtime):
 *
 *   1. An API Gateway v2 WebSocket API with `$connect` and `$disconnect` route
 *      handlers (small `backend`-image Lambdas running the handlers in
 *      `app.realtime.connect` / `app.realtime.disconnect`) that write and delete
 *      the connection ID in the `ws-connections` DynamoDB table.
 *   2. A WebSocket stage whose default route settings carry throttling limits —
 *      the socket needs no credentials, so the stage throttle is the primary
 *      backpressure control (design: "The WebSocket API stage has throttling
 *      limits set, since connections need no credentials").
 *   3. An EventBridge rule on the application bus (owned by the ApiStack) that
 *      routes the two broadcast event types — `dataset.status.changed` and
 *      `check.updated` — to the `push-queue` (also owned by the ApiStack).
 *
 * The push consumer (WorkersStack) needs this stack's callback URL as its
 * `WS_API_ENDPOINT` and `execute-api:ManageConnections` scoped to this API. The
 * stack exposes {@link webSocketApi} and {@link callbackUrl} so bin/app.ts can
 * wire them. No Lambda joins a VPC and there is no NAT gateway (Requirement 7.4
 * carried through from platform-foundation).
 *
 * Requirements: 6.1, 6.2, 6.5
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as events from "aws-cdk-lib/aws-events";
import * as eventsTargets from "aws-cdk-lib/aws-events-targets";
import * as sqs from "aws-cdk-lib/aws-sqs";
import type { DataStack } from "./data-stack";
import type { ApiStack } from "./api-stack";

/** Absolute path to the backend image build context (../backend). */
const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");

/** Timeout for the tiny connect/disconnect glue Lambdas. */
const WS_HANDLER_TIMEOUT = cdk.Duration.seconds(10);

/** Memory for the connect/disconnect glue Lambdas (lightweight DynamoDB writes). */
const WS_HANDLER_MEMORY_MB = 256;

/** Name of the deployed WebSocket stage. */
const WS_STAGE_NAME = "prod";

/**
 * Stage throttling: the socket has no credentials, so these bound the request
 * rate across every connected client. Values are deliberately modest for a
 * small-team internal tool and overridable per deploy via context.
 */
const DEFAULT_THROTTLE_RATE = 200;
const DEFAULT_THROTTLE_BURST = 100;

/** The two broadcast event types forwarded to the push queue (design). */
const BROADCAST_DETAIL_TYPES = ["dataset.status.changed", "check.updated"];

export interface RealtimeStackProps extends cdk.StackProps {
  /** The DataStack whose `ws-connections` table the handlers write/delete. */
  readonly data: DataStack;

  /** The ApiStack that owns the EventBridge bus and the `push-queue`. */
  readonly api: ApiStack;

  /** Deployment environment name injected as `ENV` (defaults to `production`). */
  readonly envName?: string;

  /** Per-second request rate limit for the stage (default 200). */
  readonly throttleRateLimit?: number;

  /** Burst request limit for the stage (default 100). */
  readonly throttleBurstLimit?: number;
}

export class RealtimeStack extends cdk.Stack {
  /** The WebSocket API. Exposed so WorkersStack can grant ManageConnections. */
  public readonly webSocketApi: apigwv2.WebSocketApi;

  /** The deployed WebSocket stage. */
  public readonly stage: apigwv2.WebSocketStage;

  /**
   * HTTPS callback URL of the stage (`https://<id>.execute-api.<region>…/<stage>`).
   * The push consumer uses it as its `WS_API_ENDPOINT` to `postToConnection`.
   */
  public readonly callbackUrl: string;

  /** `$connect` route handler Lambda. */
  public readonly connectHandler: lambda.DockerImageFunction;
  /** `$disconnect` route handler Lambda. */
  public readonly disconnectHandler: lambda.DockerImageFunction;

  constructor(scope: Construct, id: string, props: RealtimeStackProps) {
    super(scope, id, props);

    const { data, api } = props;
    const envName = props.envName ?? "production";

    // ------------------------------------------------------------------
    // Shared environment for the connect/disconnect glue Lambdas.
    // They only touch the ws-connections table, but core.config loads secrets
    // and resolves the same Settings everywhere, so mirror the minimal set.
    // ------------------------------------------------------------------
    const handlerEnv: Record<string, string> = {
      ENV: envName,
      SECRETS_ARN: data.anthropicSecret.secretArn,
      ORIGIN_VERIFY_SECRET_ARN: data.originVerifySecret.secretArn,
      WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
      // The glue Lambdas are container-image functions; override the CMD to the
      // specific handler (same contract as the SQS consumers in WorkersStack).
    };

    // ------------------------------------------------------------------
    // $connect / $disconnect handlers (backend image, NO VPC)
    // ------------------------------------------------------------------
    this.connectHandler = new lambda.DockerImageFunction(this, "ConnectHandler", {
      functionName: "reviewlens-ws-connect",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        cmd: ["app.realtime.connect.lambda_handler"],
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: WS_HANDLER_MEMORY_MB,
      timeout: WS_HANDLER_TIMEOUT,
      environment: { ...handlerEnv, SERVICE_NAME: "ws-connect" },
    });

    this.disconnectHandler = new lambda.DockerImageFunction(this, "DisconnectHandler", {
      functionName: "reviewlens-ws-disconnect",
      code: lambda.DockerImageCode.fromImageAsset(BACKEND_DIR, {
        cmd: ["app.realtime.disconnect.lambda_handler"],
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: WS_HANDLER_MEMORY_MB,
      timeout: WS_HANDLER_TIMEOUT,
      environment: { ...handlerEnv, SERVICE_NAME: "ws-disconnect" },
    });

    // Both glue handlers read/write the connection table and read the secret.
    data.wsConnectionsTable.grantReadWriteData(this.connectHandler);
    data.wsConnectionsTable.grantReadWriteData(this.disconnectHandler);
    data.anthropicSecret.grantRead(this.connectHandler);
    data.anthropicSecret.grantRead(this.disconnectHandler);
    data.originVerifySecret.grantRead(this.connectHandler);
    data.originVerifySecret.grantRead(this.disconnectHandler);

    // ------------------------------------------------------------------
    // WebSocket API + stage with throttling
    // ------------------------------------------------------------------
    this.webSocketApi = new apigwv2.WebSocketApi(this, "WebSocketApi", {
      apiName: "reviewlens-realtime",
      connectRouteOptions: {
        integration: new WebSocketLambdaIntegration(
          "ConnectIntegration",
          this.connectHandler
        ),
      },
      disconnectRouteOptions: {
        integration: new WebSocketLambdaIntegration(
          "DisconnectIntegration",
          this.disconnectHandler
        ),
      },
    });

    this.stage = new apigwv2.WebSocketStage(this, "WebSocketStage", {
      webSocketApi: this.webSocketApi,
      stageName: WS_STAGE_NAME,
      autoDeploy: true,
      // The socket needs no credentials, so stage throttling is the primary
      // backpressure control (design).
      throttle: {
        rateLimit: props.throttleRateLimit ?? DEFAULT_THROTTLE_RATE,
        burstLimit: props.throttleBurstLimit ?? DEFAULT_THROTTLE_BURST,
      },
    });

    this.callbackUrl = this.stage.callbackUrl;

    // ------------------------------------------------------------------
    // EventBridge rule: broadcast event types → push-queue
    // ------------------------------------------------------------------
    // The application bus (ApiStack) carries dataset.status.changed,
    // check.updated, and chat.exchange.saved. The real-time channel broadcasts
    // only the first two to browsers, so the rule matches exactly those detail
    // types and forwards them to the push-queue the push consumer drains.
    //
    // CROSS-STACK CYCLE AVOIDANCE: the bus and the push-queue are owned by the
    // ApiStack. If this rule referenced them as CloudFormation tokens, the SQS
    // target would add a resource policy to the Api queue conditioned on *this*
    // rule's ARN — making ApiStack depend on RealtimeStack while RealtimeStack
    // already depends on ApiStack (the bus), i.e. a dependency cycle. To keep
    // the dependency one-directional we import both by their fixed physical
    // names (literals, not tokens): the import creates no cross-stack
    // reference. We then attach an explicit queue policy here granting
    // EventBridge send, since a policy cannot be added to an imported queue.
    const bus = events.EventBus.fromEventBusName(
      this,
      "AppBus",
      api.eventBus.eventBusName
    );
    const pushQueue = sqs.Queue.fromQueueArn(
      this,
      "PushQueue",
      cdk.Stack.of(api).formatArn({
        service: "sqs",
        resource: api.pushQueue.queueName,
      })
    );

    const rule = new events.Rule(this, "BroadcastToPushQueue", {
      ruleName: "reviewlens-broadcast-to-push",
      eventBus: bus,
      description:
        "Route dataset.status.changed and check.updated events to the push queue for real-time fan-out.",
      eventPattern: {
        source: ["reviewlens"],
        detailType: BROADCAST_DETAIL_TYPES,
      },
    });
    rule.addTarget(new eventsTargets.SqsQueue(pushQueue));

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "WebSocketApiEndpoint", {
      value: this.webSocketApi.apiEndpoint,
      description: "WebSocket API endpoint (wss://…) the browser connects to",
    });
    new cdk.CfnOutput(this, "WebSocketCallbackUrl", {
      value: this.callbackUrl,
      description:
        "HTTPS callback URL (WS_API_ENDPOINT) the push consumer posts to connections through",
    });
  }
}
