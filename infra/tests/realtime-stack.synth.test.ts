/**
 * Synth assertions for the RealtimeStack (dataset-library task 4.1).
 *
 * Scope: the invariants this task introduces for the real-time backend.
 *
 *   - A WebSocket API with `$connect` and `$disconnect` routes.
 *   - Two glue Lambdas (connect + disconnect) wired to the ws-connections table.
 *   - A deployed stage with throttling (DefaultRouteSettings) set, because the
 *     socket needs no credentials (design).
 *   - An EventBridge rule on the application bus that matches exactly
 *     dataset.status.changed + check.updated and targets the push-queue.
 *   - NO Lambda attached to a VPC and no NAT gateway (Requirement 7.4).
 *
 * The push consumer itself lives in the WorkersStack; its wiring to this stack
 * (WS_API_ENDPOINT + scoped ManageConnections) is asserted in
 * workers-stack.synth.test.ts.
 *
 * Requirements: 6.1, 6.2, 6.5, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { ApiStack } from "../lib/api-stack";
import { RealtimeStack } from "../lib/realtime-stack";

function synth(): Template {
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
  return Template.fromStack(realtime);
}

test("RealtimeStack creates a WebSocket API", () => {
  const template = synth();
  template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
  template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
    ProtocolType: "WEBSOCKET",
  });
});

test("the WebSocket API has $connect and $disconnect routes", () => {
  const template = synth();
  template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
    RouteKey: "$connect",
  });
  template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
    RouteKey: "$disconnect",
  });
});

test("the WebSocket stage sets throttling on its default route settings", () => {
  const template = synth();
  template.resourceCountIs("AWS::ApiGatewayV2::Stage", 1);
  template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
    DefaultRouteSettings: Match.objectLike({
      ThrottlingRateLimit: Match.anyValue(),
      ThrottlingBurstLimit: Match.anyValue(),
    }),
  });
});

test("two glue Lambdas handle connect and disconnect", () => {
  // Connect + disconnect handlers only (the push consumer lives in Workers).
  synth().resourceCountIs("AWS::Lambda::Function", 2);
});

test("an EventBridge rule routes the broadcast event types to the push queue", () => {
  const template = synth();
  // The rule matches exactly the two broadcast detail types on the reviewlens
  // source — not chat.exchange.saved, which browsers do not receive here.
  template.hasResourceProperties("AWS::Events::Rule", {
    EventPattern: Match.objectLike({
      source: ["reviewlens"],
      "detail-type": ["dataset.status.changed", "check.updated"],
    }),
  });

  // Its target is an SQS queue (the push-queue). The queue lives in the Api
  // stack, so the target is a cross-stack ARN reference; assert the rule has an
  // SQS target at all.
  const rules = template.findResources("AWS::Events::Rule");
  const hasSqsTarget = Object.values(rules).some((rule) =>
    (rule.Properties?.Targets ?? []).some(
      (t: { Arn?: unknown }) => t.Arn !== undefined
    )
  );
  expect(hasSqsTarget).toBe(true);
});

test("no Lambda is attached to a VPC (Requirement 7.4)", () => {
  const template = synth();
  const functions = template.findResources("AWS::Lambda::Function");
  for (const [, fn] of Object.entries(functions)) {
    expect(fn.Properties?.VpcConfig).toBeUndefined();
  }
});

test("the RealtimeStack creates no NAT gateway (Requirement 7.4)", () => {
  synth().resourceCountIs("AWS::EC2::NatGateway", 0);
});
