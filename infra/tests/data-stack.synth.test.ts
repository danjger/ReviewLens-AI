/**
 * Minimal synth sanity check for the DataStack (platform-foundation task 6.1).
 *
 * The full assertion suite (bucket encryption, blocked public access,
 * lifecycle rules, no NAT gateway, DLQs, etc.) is task 6.9. This test only
 * confirms the stack synthesizes and emits the core resources so the app
 * stays buildable while later stacks are added.
 */
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";

function synth(): Template {
  const app = new cdk.App();
  const stack = new DataStack(app, "TestData", { destroyableData: true });
  return Template.fromStack(stack);
}

test("DataStack synthesizes with the core persistence resources", () => {
  const template = synth();

  // Aurora cluster with the Data API enabled, Serverless v2 min 0 ACU.
  template.hasResourceProperties("AWS::RDS::DBCluster", {
    Engine: "aurora-postgresql",
    EnableHttpEndpoint: true,
    ServerlessV2ScalingConfiguration: { MinCapacity: 0, MaxCapacity: 4 },
  });

  // Private, SSE-S3-encrypted bucket with checks/ and uploads/ expiry rules.
  template.hasResourceProperties("AWS::S3::Bucket", {
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });

  // Three DynamoDB tables, on-demand.
  template.resourceCountIs("AWS::DynamoDB::Table", 3);

  // No NAT gateway (Requirement 7.4).
  template.resourceCountIs("AWS::EC2::NatGateway", 0);
});
