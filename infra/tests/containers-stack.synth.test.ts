/**
 * Synth assertions for the ContainersStack (platform-foundation task 6.10,
 * container compute mode).
 *
 * Confirms the optional ECS Fargate compute tier synthesizes with the shape the
 * design and requirements call for:
 *   - Its OWN VPC with public subnets and NO NAT gateway (Requirement 7.4) —
 *     Fargate reaches the internet via the internet gateway + public IP.
 *   - The EventBridge bus and the three SQS queues (each with a DLQ,
 *     maxReceiveCount 3) duplicated from ApiStack so container mode is
 *     self-contained (ApiStack is not created in this mode).
 *   - Five Fargate services (API, chat, check worker, processing worker, push
 *     consumer) using the same images with command overrides, plus a sixth
 *     task definition for the sweeper scheduled task.
 *   - One internet-facing ALB with a /api/chat/* path rule (api + chat share it).
 *   - CPU autoscaling for the HTTP services and queue-depth autoscaling for the
 *     workers.
 *   - An EventBridge Scheduler running the sweeper as a Fargate RunTask every
 *     5 minutes.
 *   - ContainersStack satisfies ComputeTier (apiDomain === chatDomain === ALB).
 *
 * Requirements: 3.2, 3.9, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { ContainersStack } from "../lib/containers-stack";
import type { ComputeTier } from "../lib/compute-mode";

const ENV = { account: "111111111111", region: "us-east-1" };

function build(): { stack: ContainersStack; template: Template } {
  const app = new cdk.App();
  const data = new DataStack(app, "TestData", { destroyableData: true, env: ENV });
  const stack = new ContainersStack(app, "TestContainers", {
    env: ENV,
    data,
    envName: "test",
  });
  return { stack, template: Template.fromStack(stack) };
}

test("ContainersStack creates its own VPC with NO NAT gateway (Req 7.4)", () => {
  const { template } = build();
  template.resourceCountIs("AWS::EC2::NatGateway", 0);
  template.resourceCountIs("AWS::EC2::VPC", 1);
  // Public subnets provide egress via the internet gateway (no NAT).
  template.resourceCountIs("AWS::EC2::InternetGateway", 1);
});

test("the ContainersStack VPC uses public subnets (MapPublicIpOnLaunch true)", () => {
  const { template } = build();
  const subnets = template.findResources("AWS::EC2::Subnet");
  expect(Object.keys(subnets).length).toBeGreaterThan(0);
  for (const [, subnet] of Object.entries(subnets)) {
    expect(subnet.Properties?.MapPublicIpOnLaunch).toBe(true);
  }
});

test("the EventBridge bus and queues are duplicated from ApiStack", () => {
  const { template } = build();
  template.hasResourceProperties("AWS::Events::EventBus", { Name: "reviewlens" });
  // 3 primary queues + 3 DLQs = 6.
  template.resourceCountIs("AWS::SQS::Queue", 6);
  for (const name of ["check-queue", "processing-queue.fifo", "push-queue"]) {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: name,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
    });
  }
});

test("five Fargate services and an ECS cluster are defined", () => {
  const { template } = build();
  template.resourceCountIs("AWS::ECS::Cluster", 1);
  // api, chat, check worker, processing worker, push consumer.
  template.resourceCountIs("AWS::ECS::Service", 5);
  // 5 services + the sweeper scheduled task definition = 6.
  template.resourceCountIs("AWS::ECS::TaskDefinition", 6);
});

test("every Fargate service assigns a public IP (egress without NAT)", () => {
  const { template } = build();
  const services = template.findResources("AWS::ECS::Service");
  for (const [, svc] of Object.entries(services)) {
    const config =
      svc.Properties?.NetworkConfiguration?.AwsvpcConfiguration;
    expect(config?.AssignPublicIp).toBe("ENABLED");
  }
});

test("one internet-facing ALB fronts api + chat with a /api/chat/* path rule", () => {
  const { template } = build();
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 1);
  template.hasResourceProperties(
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    { Scheme: "internet-facing" }
  );
  // Two target groups: API and chat.
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::TargetGroup", 2);
  // The chat path rule routes /api/chat/* to the chat target group.
  template.hasResourceProperties("AWS::ElasticLoadBalancingV2::ListenerRule", {
    Conditions: Match.arrayWith([
      Match.objectLike({
        Field: "path-pattern",
        PathPatternConfig: { Values: ["/api/chat/*"] },
      }),
    ]),
  });
});

test("target groups health-check /readyz", () => {
  const { template } = build();
  const tgs = template.findResources(
    "AWS::ElasticLoadBalancingV2::TargetGroup"
  );
  for (const [, tg] of Object.entries(tgs)) {
    expect(tg.Properties?.HealthCheckPath).toBe("/readyz");
  }
});

test("HTTP services use CPU autoscaling, workers use queue-depth scaling", () => {
  const { template } = build();
  // api CPU, chat CPU, check queue-depth, processing queue-depth, push queue-depth.
  template.resourceCountIs("AWS::ApplicationAutoScaling::ScalableTarget", 5);

  // At least one target-tracking (CPU) policy for the HTTP services.
  template.hasResourceProperties(
    "AWS::ApplicationAutoScaling::ScalingPolicy",
    {
      PolicyType: "TargetTrackingScaling",
      TargetTrackingScalingPolicyConfiguration: Match.objectLike({
        PredefinedMetricSpecification: {
          PredefinedMetricType: "ECSServiceAverageCPUUtilization",
        },
      }),
    }
  );
  // At least one step-scaling policy (the queue-depth workers).
  template.hasResourceProperties(
    "AWS::ApplicationAutoScaling::ScalingPolicy",
    { PolicyType: "StepScaling" }
  );
});

test("the sweeper runs as a Fargate RunTask every 5 minutes", () => {
  const { template } = build();
  template.resourceCountIs("AWS::Scheduler::Schedule", 1);
  template.hasResourceProperties("AWS::Scheduler::Schedule", {
    ScheduleExpression: "rate(5 minutes)",
    Target: Match.objectLike({
      EcsParameters: Match.objectLike({
        LaunchType: "FARGATE",
      }),
    }),
  });
});

test("ContainersStack satisfies ComputeTier with the ALB as both origins", () => {
  const { stack } = build();
  const tier: ComputeTier = stack;
  expect(tier.apiDomain).toBe(stack.albDomain);
  expect(tier.chatDomain).toBe(stack.albDomain);
});
