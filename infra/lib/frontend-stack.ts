/**
 * FrontendStack – the S3 origin that holds the built React SPA.
 *
 * platform-foundation task 6.6. Requirement 7.2: "The static frontend SHALL be
 * served from the CDN." The SPA is a static Vite build (see the design's
 * Technology-choices table: "React + TypeScript + Vite, served from S3 through
 * CloudFront"). This stack owns only the private bucket that holds those
 * assets; CloudFront reads it as the default-behavior origin (see the
 * Architecture diagram: `CF -->|/| SPA[S3: React SPA]`).
 *
 * The EdgeStack (task 6.2) consumes this bucket via its optional `spaBucket`
 * prop and fronts it with Origin Access Control (OAC). This stack creates a
 * PRIVATE bucket (public access fully blocked) and owns the OAC bucket policy
 * that grants CloudFront read access (see below for why the policy lives here).
 *
 * This stack is compute-mode-agnostic: it always deploys, identically in Lambda
 * and (future) container mode (see compute-mode.ts).
 *
 * ──────────────────────────────────────────────────────────────────────────
 * REGION / cross-stack OAC: CloudFront is global and OAC can reference an S3
 * bucket in ANY region, but a CDK cross-stack reference (EdgeStack reading
 * `frontend.bucket`) only resolves at synth when both stacks share the same
 * environment (account + region). The EdgeStack is pinned to the CloudFront
 * WebACL region (us-east-1). bin/app.ts therefore creates THIS stack in the
 * same region as EdgeStack so the `spaBucket` token resolves cleanly without a
 * cross-region export. See bin/app.ts for the wiring.
 *
 * BREAKING THE OAC DEPENDENCY CYCLE: CloudFront's default OAC bucket policy
 * (added by `S3BucketOrigin.withOriginAccessControl`) conditions on
 * `AWS:SourceArn = <that distribution's ARN>`. If that policy were written into
 * THIS stack, it would reference the Edge distribution (Frontend → Edge) while
 * Edge already references this bucket's domain (Edge → Frontend) — a cyclic
 * stack dependency CDK rejects. To break it, EdgeStack treats `spaBucket` as an
 * imported bucket (so CDK adds no policy from the Edge side), and this stack
 * owns the OAC read policy, scoped by `aws:SourceAccount` (this account) rather
 * than the specific distribution ARN. That removes the reverse reference while
 * keeping the grant secure: only CloudFront acting in this account, over an
 * OAC-signed request, can read the objects.
 *
 * This stack creates ONLY the bucket infrastructure. Building and uploading the
 * SPA assets is the deploy pipeline's job (task 7.2), which reads the exported
 * bucket name.
 *
 * Requirements: 7.2
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";

export interface FrontendStackProps extends cdk.StackProps {
  /**
   * When true, the SPA bucket uses a DESTROY removal policy and auto-deletes
   * its objects on teardown, so a throwaway environment (dev/CI) does not block
   * on a non-empty bucket. Defaults to false (RETAIN) to protect a real
   * deployment. Mirrors DataStack's `destroyableData` flag.
   */
  readonly destroyableData?: boolean;
}

export class FrontendStack extends cdk.Stack {
  /**
   * Private S3 bucket holding the built SPA assets. Consumed by EdgeStack's
   * `spaBucket` prop and fronted via Origin Access Control.
   */
  public readonly bucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: FrontendStackProps = {}) {
    super(scope, id, props);

    const destroyable = props.destroyableData ?? false;
    const removalPolicy = destroyable
      ? cdk.RemovalPolicy.DESTROY
      : cdk.RemovalPolicy.RETAIN;

    // ------------------------------------------------------------------
    // Private SPA bucket (Requirement 7.2)
    // ------------------------------------------------------------------
    // Fully private: CloudFront reads it through Origin Access Control, which
    // EdgeStack configures. No public access, SSE-S3 at rest, SSL enforced —
    // matching the DataStack application bucket's posture.
    this.bucket = new s3.Bucket(this, "SpaBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy,
      autoDeleteObjects: destroyable,
    });

    // ------------------------------------------------------------------
    // Origin Access Control read grant (cycle-free, see class doc)
    // ------------------------------------------------------------------
    // Allow the CloudFront service principal to read objects, but ONLY when the
    // request originates from a CloudFront distribution in THIS account (OAC
    // signs requests with the distribution's SourceArn). Scoping by
    // `AWS:SourceAccount` instead of the specific distribution ARN avoids a
    // cross-stack reference back to the Edge distribution, so the Frontend and
    // Edge stacks have a single-direction dependency (Edge → Frontend).
    this.bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AllowCloudFrontOacRead",
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal("cloudfront.amazonaws.com")],
        actions: ["s3:GetObject"],
        resources: [this.bucket.arnForObjects("*")],
        conditions: {
          StringEquals: { "AWS:SourceAccount": this.account },
        },
      })
    );

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    // The deploy pipeline (task 7.2) reads this to upload the built SPA, and it
    // identifies the CloudFront default-behavior origin.
    new cdk.CfnOutput(this, "SpaBucketName", {
      value: this.bucket.bucketName,
      description: "S3 bucket holding the built React SPA (CloudFront origin)",
    });
  }
}
