/**
 * TestFixturesStack – the public test-only fixtures site.
 *
 * platform-foundation task 6.8. Requirement 8.6: test environments serve
 * fixture review pages the SSRF protection allows — "a public static fixture
 * site for tests against deployed stacks". The design's Testing Strategy
 * section is explicit: "SSRF protection blocks private addresses, so fixture
 * pages served from `localhost` would always be refused. Deployed stacks point
 * tests at `/fixtures-site`, published to a separate public S3 + CloudFront
 * test site." (Local runs instead set `SSRF_TEST_ALLOW_HOSTS=fixtures` for the
 * compose fixture container; that path never involves this stack.)
 *
 * This stack publishes the static pages in `/fixtures-site` to an S3 bucket and
 * fronts them with a CloudFront distribution. Tests read the distribution
 * domain (exported as a CfnOutput) as the fixture-site base URL and fetch pages
 * over HTTPS — a PUBLIC host that passes `assert_public_host`, unlike a private
 * `localhost` origin.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * NON-PRODUCTION ONLY (Requirement 8.6): this is a test-only resource. It MUST
 * NOT be created in a production account. The gate lives in bin/app.ts, which
 * instantiates this stack ONLY when `envName !== "production"`. By default the
 * app runs with `envName = "production"`, so the fixtures stack is ABSENT from
 * `cdk list` / `cdk synth` unless a non-production env is explicitly selected
 * (e.g. `-c envName=test`). This stack also carries no guard of its own beyond
 * that wiring — keeping the "deploy only to non-production" decision in one
 * central place alongside the other envName-aware wiring.
 *
 * BUCKET POSTURE: the task calls for a "public S3 + CloudFront site". The SITE
 * is publicly reachable through CloudFront; the BUCKET itself stays private
 * (public access fully blocked) and is read by CloudFront through Origin Access
 * Control (OAC) — the same posture as FrontendStack and the EdgeStack SPA
 * origin. Public static-website hosting on the bucket is intentionally NOT used;
 * public reachability is provided by the CloudFront URL only.
 *
 * PUBLISHING: `aws-s3-deployment.BucketDeployment` with `Source.asset` pointing
 * at the `/fixtures-site` directory uploads the static files at deploy time via
 * a Lambda-backed custom resource. `Source.asset` of a plain directory is a
 * zip-only asset — it does NOT bundle in Docker — so `cdk synth` works without
 * a Docker daemon.
 *
 * THROWAWAY: being test-only, the bucket uses a DESTROY removal policy and
 * auto-deletes its objects, so a non-production environment tears down cleanly.
 *
 * Requirements: 8.6
 */
import * as cdk from "aws-cdk-lib";
import * as path from "path";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";

/**
 * Location of the static fixture pages, relative to this file
 * (`infra/lib/test-fixtures-stack.ts` → repo-root `/fixtures-site`).
 */
const FIXTURES_SITE_DIR = path.join(__dirname, "..", "..", "fixtures-site");

export interface TestFixturesStackProps extends cdk.StackProps {}

export class TestFixturesStack extends cdk.Stack {
  /** Private bucket holding the published fixture pages (read via OAC). */
  public readonly bucket: s3.Bucket;

  /** CloudFront distribution serving the fixtures publicly over HTTPS. */
  public readonly distribution: cloudfront.Distribution;

  /** Convenience: the public CloudFront domain for the fixtures site. */
  public readonly distributionDomainName: string;

  constructor(
    scope: Construct,
    id: string,
    props: TestFixturesStackProps = {}
  ) {
    super(scope, id, props);

    // ------------------------------------------------------------------
    // Private fixtures bucket (public access blocked; read via OAC)
    // ------------------------------------------------------------------
    // Same posture as FrontendStack: fully private, SSE-S3 at rest, SSL
    // enforced. CloudFront reads it through Origin Access Control. Being
    // test-only/throwaway, it uses DESTROY + autoDeleteObjects so a
    // non-production environment can be torn down without emptying it by hand.
    this.bucket = new s3.Bucket(this, "FixturesBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ------------------------------------------------------------------
    // CloudFront distribution — the PUBLIC entry point for the fixtures
    // ------------------------------------------------------------------
    // The site is publicly reachable here (tests fetch pages over HTTPS from a
    // public host, satisfying assert_public_host). The bucket stays private;
    // S3BucketOrigin.withOriginAccessControl manages the OAC read policy on the
    // bucket (it lives in THIS stack, so there is no cross-stack cycle).
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "ReviewLens AI public test-fixtures site (non-production only)",
      // Serve index.html at the root; also used for directory-style requests.
      defaultRootObject: "index.html",
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
      },
    });

    this.distributionDomainName = this.distribution.distributionDomainName;

    // ------------------------------------------------------------------
    // Publish the static fixture pages
    // ------------------------------------------------------------------
    // Source.asset of a plain directory is a zip-only asset (no Docker
    // bundling), so synth succeeds without a Docker daemon. The deployment runs
    // a Lambda-backed custom resource at deploy time that copies the files into
    // the bucket and invalidates the distribution cache.
    new s3deploy.BucketDeployment(this, "PublishFixtures", {
      sources: [s3deploy.Source.asset(FIXTURES_SITE_DIR)],
      destinationBucket: this.bucket,
      distribution: this.distribution,
      distributionPaths: ["/*"],
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    // Tests read this as the fixture-site base URL (e.g. FIXTURE_SITE_BASE_URL
    // = https://<domain>) when running against a deployed stack.
    new cdk.CfnOutput(this, "FixturesSiteDomain", {
      value: this.distributionDomainName,
      description:
        "Public CloudFront domain for the test-fixtures site (base URL for tests)",
    });
    new cdk.CfnOutput(this, "FixturesBucketName", {
      value: this.bucket.bucketName,
      description: "S3 bucket holding the published fixture pages",
    });
  }
}
