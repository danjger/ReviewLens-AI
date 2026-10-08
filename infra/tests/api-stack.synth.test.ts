/**
 * Synth assertions for the ApiStack (platform-foundation task 6.3).
 *
 * Confirms the Lambda-mode HTTP tier and the event/queue backbone synthesize
 * with the properties the design and requirements call for:
 *   - EventBridge custom bus named `reviewlens`
 *   - check-queue (standard), processing-queue.fifo (FIFO), push-queue
 *     (standard), each with a DLQ and redrive maxReceiveCount 3
 *   - FIFO queue visibility timeout = 6× the worker Lambda timeout
 *   - API service Lambda behind an HTTP API, chat Lambda with a streaming
 *     Function URL (RESPONSE_STREAM, auth NONE)
 *   - NO Lambda attached to a VPC (Requirement 7.4)
 *
 * Requirements: 1.1, 3.2, 7.1, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { ApiStack } from "../lib/api-stack";

function synth(): Template {
  const app = new cdk.App();
  const env = { account: "111111111111", region: "us-east-1" };
  const data = new DataStack(app, "TestData", { destroyableData: true, env });
  const api = new ApiStack(app, "TestApi", { env, data, envName: "test" });
  return Template.fromStack(api);
}

test("ApiStack defines the reviewlens EventBridge bus", () => {
  const template = synth();
  template.hasResourceProperties("AWS::Events::EventBus", {
    Name: "reviewlens",
  });
});

test("ApiStack defines three queues, each with a DLQ and maxReceiveCount 3", () => {
  const template = synth();

  // 3 primary queues + 3 DLQs = 6 queues.
  template.resourceCountIs("AWS::SQS::Queue", 6);

  // Every primary queue has a redrive policy capped at 3 receives.
  for (const name of ["check-queue", "processing-queue.fifo", "push-queue"]) {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: name,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
    });
  }
});

test("processing-queue and its DLQ are FIFO", () => {
  const template = synth();
  template.hasResourceProperties("AWS::SQS::Queue", {
    QueueName: "processing-queue.fifo",
    FifoQueue: true,
    ContentBasedDeduplication: false,
  });
  template.hasResourceProperties("AWS::SQS::Queue", {
    QueueName: "processing-queue-dlq.fifo",
    FifoQueue: true,
  });
});

test("queue visibility timeout is 6x the worker Lambda timeout (5 min)", () => {
  const template = synth();
  // 5 minutes * 6 = 1800 seconds.
  template.hasResourceProperties("AWS::SQS::Queue", {
    QueueName: "check-queue",
    VisibilityTimeout: 1800,
  });
});

test("the API Lambda is fronted by an HTTP API", () => {
  const template = synth();
  template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
});

test("the chat Lambda exposes a streaming Function URL with no auth", () => {
  const template = synth();
  template.hasResourceProperties("AWS::Lambda::Url", {
    AuthType: "NONE",
    InvokeMode: "RESPONSE_STREAM",
  });
});

test("the chat Lambda runs with the LWA response_stream invoke mode", () => {
  // guardrailed-chat task 4.1: in Lambda mode the chat service runs FastAPI
  // behind the AWS Lambda Web Adapter with AWS_LWA_INVOKE_MODE=response_stream
  // so the Function URL can stream the SSE answer back.
  const template = synth();
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "reviewlens-chat",
    Environment: {
      Variables: Match.objectLike({
        AWS_LWA_INVOKE_MODE: "response_stream",
        SERVICE_NAME: "chat",
      }),
    },
  });
});

test("the chat Lambda image CMD serves the app.chat FastAPI app via uvicorn", () => {
  // The chat construct overrides the image CMD so the function serves
  // app.chat:app (the streaming FastAPI service) instead of the default
  // app.api. LWA proxies to whatever listens on $PORT.
  const template = synth();
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "reviewlens-chat",
    ImageConfig: {
      Command: ["uvicorn", "app.chat:app", "--host", "0.0.0.0", "--port", "8080"],
    },
  });
});

test("the API Lambda uses buffered LWA mode (not streaming)", () => {
  // Only the chat service streams; the API service uses buffered responses.
  const template = synth();
  template.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "reviewlens-api",
    Environment: {
      Variables: Match.objectLike({
        AWS_LWA_INVOKE_MODE: "buffered",
        SERVICE_NAME: "api",
      }),
    },
  });
});

test("no Lambda is attached to a VPC (Requirement 7.4)", () => {
  const template = synth();
  const functions = template.findResources("AWS::Lambda::Function");
  for (const [, fn] of Object.entries(functions)) {
    expect(fn.Properties?.VpcConfig).toBeUndefined();
  }
});

test("the ApiStack creates no NAT gateway", () => {
  const template = synth();
  template.resourceCountIs("AWS::EC2::NatGateway", 0);
});
