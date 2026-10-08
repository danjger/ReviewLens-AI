/**
 * Synth smoke assertions for the WorkersStack (platform-foundation task 6.4).
 *
 * Scope: just the invariants this task introduces. The full assertion suite
 * (DLQ-on-every-queue, encryption, lifecycle, etc.) is task 6.9.
 *
 *   - Five Lambdas: check worker, processing worker, push consumer, DLQ
 *     consumer, sweeper.
 *   - Six SQS event-source mappings (check + processing + push + 3 DLQs), each
 *     reporting partial batch failures.
 *   - A single EventBridge Scheduler schedule at rate(5 minutes).
 *   - NO Lambda attached to a VPC and no NAT gateway (Requirement 7.4).
 *
 * Requirements: 3.4, 3.6, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { ApiStack } from "../lib/api-stack";
import { WorkersStack } from "../lib/workers-stack";
import { RealtimeStack } from "../lib/realtime-stack";

function synth(): Template {
  const app = new cdk.App();
  const env = { account: "111111111111", region: "us-east-1" };
  const data = new DataStack(app, "TestData", { destroyableData: true, env });
  const api = new ApiStack(app, "TestApi", { env, data, envName: "test" });
  const workers = new WorkersStack(app, "TestWorkers", {
    env,
    data,
    api,
    envName: "test",
  });
  return Template.fromStack(workers);
}

/** Synth the WorkersStack WITH the RealtimeStack wired, as bin/app.ts does. */
function synthWithRealtime(): Template {
  const app = new cdk.App();
  const env = { account: "111111111111", region: "us-east-1" };
  const data = new DataStack(app, "TestData", { destroyableData: true, env });
  const api = new ApiStack(app, "TestApi", { env, data, envName: "test" });
  const realtime = new RealtimeStack(app, "TestRealtime", {
    env,
    data,
    api,
    envName: "test",
  });
  const workers = new WorkersStack(app, "TestWorkers", {
    env,
    data,
    api,
    realtime,
    envName: "test",
  });
  return Template.fromStack(workers);
}

test("WorkersStack defines five worker/consumer Lambdas", () => {
  synth().resourceCountIs("AWS::Lambda::Function", 5);
});

test("every SQS event source reports partial batch failures", () => {
  const template = synth();
  // check + processing + push + 3 DLQs = 6 mappings.
  template.resourceCountIs("AWS::Lambda::EventSourceMapping", 6);
  const mappings = template.findResources("AWS::Lambda::EventSourceMapping");
  for (const [, mapping] of Object.entries(mappings)) {
    expect(mapping.Properties?.FunctionResponseTypes).toEqual([
      "ReportBatchItemFailures",
    ]);
  }
});

test("the sweeper runs on an EventBridge Scheduler every 5 minutes", () => {
  const template = synth();
  template.resourceCountIs("AWS::Scheduler::Schedule", 1);
  template.hasResourceProperties("AWS::Scheduler::Schedule", {
    ScheduleExpression: "rate(5 minutes)",
  });
});

test("no Lambda is attached to a VPC (Requirement 7.4)", () => {
  const template = synth();
  const functions = template.findResources("AWS::Lambda::Function");
  for (const [, fn] of Object.entries(functions)) {
    expect(fn.Properties?.VpcConfig).toBeUndefined();
  }
});

test("the WorkersStack creates no NAT gateway (Requirement 7.4)", () => {
  synth().resourceCountIs("AWS::EC2::NatGateway", 0);
});

test("the push consumer may manage WebSocket connections", () => {
  const template = synth();
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: "execute-api:ManageConnections",
        }),
      ]),
    }),
  });
});

test("with the RealtimeStack wired, the push consumer gets WS_API_ENDPOINT", () => {
  const template = synthWithRealtime();
  // The push consumer function carries the WebSocket callback URL so it knows
  // where to postToConnection (the value is a cross-stack token).
  const functions = template.findResources("AWS::Lambda::Function");
  const pushConsumer = Object.values(functions).find(
    (fn) => fn.Properties?.FunctionName === "reviewlens-push-consumer"
  );
  expect(pushConsumer).toBeDefined();
  expect(
    pushConsumer?.Properties?.Environment?.Variables?.WS_API_ENDPOINT
  ).toBeDefined();
});

test("with the RealtimeStack wired, ManageConnections is still granted", () => {
  const template = synthWithRealtime();
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: "execute-api:ManageConnections",
        }),
      ]),
    }),
  });
});
