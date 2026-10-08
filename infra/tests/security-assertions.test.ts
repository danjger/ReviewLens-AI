/**
 * Security / compliance CDK assertion suite (platform-foundation task 6.9).
 *
 * This is the consolidated assertion suite that `make synth` runs after
 * `cdk synth`. It instantiates every stack with a fixed test environment and
 * asserts the security- and cost-critical invariants the task names, each
 * mapped to its requirement:
 *
 *   1. Bucket encryption — DataStack app bucket, FrontendStack SPA bucket, and
 *      the TestFixtures bucket all use SSE (AES256).            (Req 5.3)
 *   2. Blocked public access — those buckets BLOCK_ALL public access. (Req 5.2)
 *   3. Lifecycle rules — the app bucket expires checks/ and uploads/ after
 *      1 day.                                                    (Req 5.1)
 *   4. WAF attached with a rate rule — EdgeStack WebACL (scope CLOUDFRONT) has
 *      a RateBasedStatement (limit present, AggregateKeyType IP), and the
 *      distribution references it via WebACLId.                  (Req 2.2)
 *   5. Origin header configured — the /api/* and /api/chat/* origins carry the
 *      X-Origin-Verify custom header.                            (Req 2.3)
 *   6. No Lambda attached to a VPC — across ApiStack and WorkersStack no
 *      AWS::Lambda::Function has VpcConfig.                      (Req 7.4)
 *   7. No NAT gateway — AWS::EC2::NatGateway count is 0 in every stack. (Req 7.4)
 *   8. DLQs on every queue — every primary queue has a RedrivePolicy with
 *      maxReceiveCount 3, and the DLQs exist.                    (Req 7.4 / design)
 *
 * Requirements: 2.2, 2.3, 5.1, 5.2, 5.3, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { EdgeStack } from "../lib/edge-stack";
import { ApiStack } from "../lib/api-stack";
import { WorkersStack } from "../lib/workers-stack";
import { FrontendStack } from "../lib/frontend-stack";
import { TestFixturesStack } from "../lib/test-fixtures-stack";

/** Fixed test environment shared by every stack (us-east-1 for the CLOUDFRONT WebACL). */
const ENV = { account: "111111111111", region: "us-east-1" };

/**
 * Build every stack once with the test env and cross-stack props wired exactly
 * as bin/app.ts does, and return a Template per stack. Each `describe` block
 * reads the stack(s) it needs.
 */
function synthAll(): {
  data: Template;
  edge: Template;
  api: Template;
  workers: Template;
  frontend: Template;
  fixtures: Template;
} {
  const app = new cdk.App();

  const data = new DataStack(app, "TestData", { destroyableData: true, env: ENV });
  const api = new ApiStack(app, "TestApi", { env: ENV, data, envName: "test" });
  const workers = new WorkersStack(app, "TestWorkers", {
    env: ENV,
    data,
    api,
    envName: "test",
  });
  const frontend = new FrontendStack(app, "TestFrontend", {
    env: ENV,
    destroyableData: true,
  });
  const edge = new EdgeStack(app, "TestEdge", {
    env: ENV,
    // Wire real-ish origin domains so the custom-header origins are the API /
    // chat origins (not the SPA, which uses OAC and no header).
    apiDomain: "api.example.com",
    chatFunctionUrlDomain: "chat.example.com",
    spaBucket: frontend.bucket,
    originVerifySecretValue: "test-origin-verify-value",
  });
  const fixtures = new TestFixturesStack(app, "TestFixtures", { env: ENV });

  return {
    data: Template.fromStack(data),
    edge: Template.fromStack(edge),
    api: Template.fromStack(api),
    workers: Template.fromStack(workers),
    frontend: Template.fromStack(frontend),
    fixtures: Template.fromStack(fixtures),
  };
}

const templates = synthAll();

// ---------------------------------------------------------------------------
// 1. Bucket encryption (Requirement 5.3)
// ---------------------------------------------------------------------------
describe("bucket encryption at rest (Req 5.3)", () => {
  const sseAes256 = {
    BucketEncryption: {
      ServerSideEncryptionConfiguration: Match.arrayWith([
        Match.objectLike({
          ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" },
        }),
      ]),
    },
  };

  test("the DataStack application bucket uses SSE (AES256)", () => {
    templates.data.hasResourceProperties("AWS::S3::Bucket", sseAes256);
  });

  test("the FrontendStack SPA bucket uses SSE (AES256)", () => {
    templates.frontend.hasResourceProperties("AWS::S3::Bucket", sseAes256);
  });

  test("the TestFixtures bucket uses SSE (AES256)", () => {
    templates.fixtures.hasResourceProperties("AWS::S3::Bucket", sseAes256);
  });

  test("no bucket in any stack is left unencrypted", () => {
    for (const t of [templates.data, templates.frontend, templates.fixtures]) {
      const buckets = t.findResources("AWS::S3::Bucket");
      for (const [, bucket] of Object.entries(buckets)) {
        expect(
          bucket.Properties?.BucketEncryption?.ServerSideEncryptionConfiguration
        ).toBeDefined();
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 2. Blocked public access (Requirement 5.2)
// ---------------------------------------------------------------------------
describe("blocked public access on buckets (Req 5.2)", () => {
  const blockAll = {
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  };

  test("the DataStack application bucket blocks all public access", () => {
    templates.data.hasResourceProperties("AWS::S3::Bucket", blockAll);
  });

  test("the FrontendStack SPA bucket blocks all public access", () => {
    templates.frontend.hasResourceProperties("AWS::S3::Bucket", blockAll);
  });

  test("the TestFixtures bucket blocks all public access", () => {
    templates.fixtures.hasResourceProperties("AWS::S3::Bucket", blockAll);
  });

  test("every bucket in these stacks sets BLOCK_ALL", () => {
    for (const t of [templates.data, templates.frontend, templates.fixtures]) {
      const buckets = t.findResources("AWS::S3::Bucket");
      for (const [, bucket] of Object.entries(buckets)) {
        expect(bucket.Properties?.PublicAccessBlockConfiguration).toEqual({
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        });
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 3. Lifecycle rules (Requirement 5.1)
// ---------------------------------------------------------------------------
describe("temporary-prefix lifecycle rules on the app bucket (Req 5.1)", () => {
  test("checks/ objects expire after 1 day", () => {
    templates.data.hasResourceProperties("AWS::S3::Bucket", {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Prefix: "checks/",
            ExpirationInDays: 1,
            Status: "Enabled",
          }),
        ]),
      },
    });
  });

  test("uploads/ objects expire after 1 day", () => {
    templates.data.hasResourceProperties("AWS::S3::Bucket", {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Prefix: "uploads/",
            ExpirationInDays: 1,
            Status: "Enabled",
          }),
        ]),
      },
    });
  });
});

// ---------------------------------------------------------------------------
// 4. WAF attached with a per-IP rate rule (Requirement 2.2)
// ---------------------------------------------------------------------------
describe("WAF web ACL with a per-IP rate rule (Req 2.2)", () => {
  test("a CLOUDFRONT-scoped WebACL exists", () => {
    templates.edge.resourceCountIs("AWS::WAFv2::WebACL", 1);
    templates.edge.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "CLOUDFRONT",
    });
  });

  test("the WebACL carries a RateBasedStatement aggregating by IP", () => {
    templates.edge.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.arrayWith([
        Match.objectLike({
          Statement: {
            RateBasedStatement: Match.objectLike({
              Limit: Match.anyValue(),
              AggregateKeyType: "IP",
            }),
          },
          Action: { Block: {} },
        }),
      ]),
    });
  });

  test("the CloudFront distribution references the WebACL (WebACLId set)", () => {
    templates.edge.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        WebACLId: Match.anyValue(),
      }),
    });
  });
});

// ---------------------------------------------------------------------------
// 5. Origin header configured (Requirement 2.3)
// ---------------------------------------------------------------------------
describe("X-Origin-Verify custom origin header on the API/chat origins (Req 2.3)", () => {
  test("at least one origin carries the X-Origin-Verify custom header", () => {
    templates.edge.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        Origins: Match.arrayWith([
          Match.objectLike({
            OriginCustomHeaders: Match.arrayWith([
              Match.objectLike({
                HeaderName: "X-Origin-Verify",
                HeaderValue: Match.anyValue(),
              }),
            ]),
          }),
        ]),
      }),
    });
  });

  test("the API and chat origins both carry X-Origin-Verify (two origins)", () => {
    const distributions = templates.edge.findResources(
      "AWS::CloudFront::Distribution"
    );
    const [distribution] = Object.values(distributions);
    const origins: Array<{ OriginCustomHeaders?: Array<{ HeaderName: string }> }> =
      distribution.Properties.DistributionConfig.Origins;

    const withVerifyHeader = origins.filter((o) =>
      (o.OriginCustomHeaders ?? []).some(
        (h) => h.HeaderName === "X-Origin-Verify"
      )
    );
    // The API Gateway origin and the chat Function URL origin both carry it;
    // the SPA (OAC) origin does not.
    expect(withVerifyHeader.length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// 6. No Lambda attached to a VPC (Requirement 7.4)
// ---------------------------------------------------------------------------
describe("no Lambda is attached to a VPC (Req 7.4)", () => {
  test("no AWS::Lambda::Function in the ApiStack has VpcConfig", () => {
    const functions = templates.api.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(functions)) {
      expect(fn.Properties?.VpcConfig).toBeUndefined();
    }
  });

  test("no AWS::Lambda::Function in the WorkersStack has VpcConfig", () => {
    const functions = templates.workers.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(functions)) {
      expect(fn.Properties?.VpcConfig).toBeUndefined();
    }
  });
});

// ---------------------------------------------------------------------------
// 7. No NAT gateway (Requirement 7.4)
// ---------------------------------------------------------------------------
describe("no NAT gateway in any stack (Req 7.4)", () => {
  test("every stack synthesizes zero NAT gateways", () => {
    for (const t of [
      templates.data,
      templates.edge,
      templates.api,
      templates.workers,
      templates.frontend,
      templates.fixtures,
    ]) {
      t.resourceCountIs("AWS::EC2::NatGateway", 0);
    }
  });

  test("the DataStack VPC uses only isolated subnets (no public subnets)", () => {
    // Isolated subnets have MapPublicIpOnLaunch=false; a public subnet would be
    // true. Assert none are public, which also implies no need for a NAT/IGW.
    const subnets = templates.data.findResources("AWS::EC2::Subnet");
    expect(Object.keys(subnets).length).toBeGreaterThan(0);
    for (const [, subnet] of Object.entries(subnets)) {
      expect(subnet.Properties?.MapPublicIpOnLaunch).not.toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// 8. DLQs on every queue (Requirement 7.4 / design Error Handling)
// ---------------------------------------------------------------------------
describe("every primary queue has a DLQ with maxReceiveCount 3", () => {
  const PRIMARY_QUEUES = [
    "check-queue",
    "processing-queue.fifo",
    "push-queue",
  ];

  test("3 primary queues + 3 DLQs = 6 SQS queues exist", () => {
    templates.api.resourceCountIs("AWS::SQS::Queue", 6);
  });

  test("each primary queue has a RedrivePolicy pointing at a DLQ (maxReceiveCount 3)", () => {
    for (const name of PRIMARY_QUEUES) {
      templates.api.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: name,
        RedrivePolicy: Match.objectLike({
          maxReceiveCount: 3,
          deadLetterTargetArn: Match.anyValue(),
        }),
      });
    }
  });

  test("the matching DLQs exist (one per primary queue)", () => {
    for (const dlq of [
      "check-queue-dlq",
      "processing-queue-dlq.fifo",
      "push-queue-dlq",
    ]) {
      templates.api.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: dlq,
      });
    }
  });

  test("every queue that is NOT a DLQ carries a RedrivePolicy", () => {
    const queues = templates.api.findResources("AWS::SQS::Queue");
    for (const [, queue] of Object.entries(queues)) {
      const name: string | undefined = queue.Properties?.QueueName;
      const isDlq = typeof name === "string" && name.includes("-dlq");
      if (!isDlq) {
        expect(queue.Properties?.RedrivePolicy).toBeDefined();
        expect(queue.Properties?.RedrivePolicy?.maxReceiveCount).toBe(3);
      }
    }
  });
});
