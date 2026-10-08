/**
 * EdgeStack – the single public entry point for ReviewLens AI.
 *
 * platform-foundation task 6.2. Provides:
 *   - A CloudFront distribution with three cache behaviors:
 *       `/`             → the SPA (private S3 bucket via Origin Access Control)
 *       `/api/*`        → the API Gateway HTTP API origin
 *       `/api/chat/*`   → the Chat service Function URL origin (streaming)
 *   - An AWS WAF WebACL (scope CLOUDFRONT) with a per-IP rate-based rule
 *     (default 300 requests / 5 minutes) and the AWS managed common rule set.
 *   - A secret `X-Origin-Verify` custom origin header added to the API and
 *     chat origins so the backend `core.origin_guard` middleware can reject any
 *     request that did not come through CloudFront (Requirement 2.3).
 *
 * Requirements: 1.1, 2.2, 2.3, 7.2
 *
 * ──────────────────────────────────────────────────────────────────────────
 * REGION: CloudFront WAF WebACLs (scope CLOUDFRONT) MUST live in us-east-1.
 * CloudFront itself is global. Deploy this stack to us-east-1 (see bin/app.ts,
 * which pins `env.region = "us-east-1"` for the Edge stack).
 *
 * ORIGINS NOT YET BUILT: the API Gateway HTTP API (task 6.3), the Chat
 * Function URL (task 6.3), and the SPA S3 bucket (task 6.6) do not exist yet.
 * To let `cdk synth` succeed standalone today while allowing the real wiring to
 * tighten later, this stack accepts every origin endpoint as an OPTIONAL prop:
 *
 *   - `apiDomain`              – domain of the API Gateway HTTP API
 *   - `chatFunctionUrlDomain`  – domain of the Chat service Function URL
 *   - `spaBucket`              – the SPA S3 bucket (import or L2 ref)
 *
 * When a prop is omitted, a safe placeholder is used (a `placeholder.invalid`
 * HTTP origin for the API/chat domains, and a freshly-created private bucket
 * for the SPA) so the template is valid. bin/app.ts documents the wiring; once
 * tasks 6.3/6.6 exist their outputs are passed in to replace the placeholders.
 *
 * ORIGIN-VERIFY SECRET: CloudFront custom origin headers must be plaintext in
 * the synthesized template, so the secret VALUE cannot be a CloudFormation
 * dynamic reference to Secrets Manager (CloudFront rejects unresolved tokens
 * for custom headers). We therefore accept the value as a prop
 * (`originVerifySecretValue`) that the deploy pipeline resolves from the
 * DataStack `ORIGIN_VERIFY_SECRET` secret at deploy time (e.g. read via the CLI
 * and passed through `-c originVerifySecret=...` or an environment variable) —
 * it is NEVER hardcoded in source. When omitted, a clearly-marked synth-only
 * placeholder is used so `cdk synth` works; that placeholder must not be
 * deployed. See bin/app.ts for how the value is injected.
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";

/** Placeholder domain used when a real origin is not wired yet. */
const PLACEHOLDER_ORIGIN_DOMAIN = "placeholder.invalid";

/**
 * Synth-only placeholder for the X-Origin-Verify secret. Clearly marked so a
 * misconfigured deploy is obvious. The deploy pipeline MUST pass the real value
 * resolved from Secrets Manager; this constant is never a real secret.
 */
const PLACEHOLDER_ORIGIN_VERIFY = "SYNTH-ONLY-PLACEHOLDER-DO-NOT-DEPLOY";

/** Default WAF rate-based limit: requests per IP per 5-minute window. */
const DEFAULT_RATE_LIMIT_PER_5_MIN = 300;

export interface EdgeStackProps extends cdk.StackProps {
  /**
   * Domain name of the API Gateway HTTP API origin (task 6.3). When omitted a
   * placeholder origin is used so synth succeeds standalone.
   */
  readonly apiDomain?: string;

  /**
   * Domain name of the Chat service Function URL origin (task 6.3). A Function
   * URL domain looks like `<id>.lambda-url.<region>.on.aws`. When omitted a
   * placeholder origin is used so synth succeeds standalone.
   */
  readonly chatFunctionUrlDomain?: string;

  /**
   * The SPA S3 bucket (task 6.6, FrontendStack). When omitted this stack
   * creates a private placeholder bucket so the default behavior has an origin
   * and synth succeeds standalone.
   */
  readonly spaBucket?: s3.IBucket;

  /**
   * Plaintext value of the X-Origin-Verify secret, resolved from the DataStack
   * `ORIGIN_VERIFY_SECRET` secret by the deploy pipeline. Required for a real
   * deploy; when omitted a synth-only placeholder is used. NEVER hardcode a
   * real value here.
   */
  readonly originVerifySecretValue?: string;

  /**
   * WAF rate-based rule limit (requests per IP per 5 minutes). Defaults to 300
   * per the design's Error Handling section.
   */
  readonly rateLimitPer5Min?: number;
}

export class EdgeStack extends cdk.Stack {
  /** The CloudFront distribution – the single public entry point. */
  public readonly distribution: cloudfront.Distribution;

  /** The WAF WebACL attached to the distribution. */
  public readonly webAcl: wafv2.CfnWebACL;

  /** Convenience: the distribution's public domain name. */
  public readonly distributionDomainName: string;

  constructor(scope: Construct, id: string, props: EdgeStackProps = {}) {
    super(scope, id, props);

    const rateLimit = props.rateLimitPer5Min ?? DEFAULT_RATE_LIMIT_PER_5_MIN;
    const originVerifyValue =
      props.originVerifySecretValue ?? PLACEHOLDER_ORIGIN_VERIFY;

    // ------------------------------------------------------------------
    // AWS WAF WebACL (scope CLOUDFRONT → must be in us-east-1)
    // ------------------------------------------------------------------
    // Requirement 2.2: all public traffic enters through a single CDN
    // distribution protected by a WAF with a per-IP request rate rule.
    this.webAcl = new wafv2.CfnWebACL(this, "WebAcl", {
      // CLOUDFRONT scope WebACLs are global and only valid in us-east-1.
      scope: "CLOUDFRONT",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        sampledRequestsEnabled: true,
        metricName: `${id}-webacl`,
      },
      rules: [
        // Per-IP rate-based rule. Blocks an IP that exceeds `rateLimit`
        // requests in any trailing 5-minute window (WAF's fixed evaluation
        // window). Default 300 per the design's Error Handling section.
        {
          name: "PerIpRateLimit",
          priority: 0,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: rateLimit,
              aggregateKeyType: "IP",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            sampledRequestsEnabled: true,
            metricName: `${id}-per-ip-rate`,
          },
        },
        // AWS managed common rule set (OWASP-style common protections).
        {
          name: "AWSManagedRulesCommonRuleSet",
          priority: 1,
          // Use the managed rule group's own actions; count nothing here.
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            sampledRequestsEnabled: true,
            metricName: `${id}-common-rules`,
          },
        },
      ],
    });

    // ------------------------------------------------------------------
    // Origins
    // ------------------------------------------------------------------
    // SPA origin: private S3 bucket served via Origin Access Control (OAC).
    // Provided by FrontendStack (task 6.6); a private placeholder bucket is
    // created here when not yet wired so the default behavior has an origin.
    //
    // CROSS-STACK CYCLE: `S3BucketOrigin.withOriginAccessControl` normally adds
    // a bucket policy that conditions on THIS distribution's ARN. When the real
    // SPA bucket lives in another stack (FrontendStack), writing that policy
    // into the bucket's stack would create a Frontend → Edge reference while
    // Edge already references the bucket's domain (Edge → Frontend) — a cyclic
    // stack dependency. To break it, we re-import a cross-stack `spaBucket` by
    // its attributes so CDK does NOT attempt to mutate its policy from here;
    // FrontendStack owns the account-scoped OAC read policy instead. The
    // standalone placeholder bucket is created in this stack, so OAC manages its
    // policy here as usual.
    let spaBucket: s3.IBucket;
    if (props.spaBucket) {
      // Import by attributes: an imported bucket is treated as read-only by
      // CDK, so withOriginAccessControl adds no policy (it emits a warning,
      // which is expected — the policy is owned by FrontendStack).
      spaBucket = s3.Bucket.fromBucketAttributes(this, "ImportedSpaBucket", {
        bucketName: props.spaBucket.bucketName,
        region: props.spaBucket.stack.region,
      });
    } else {
      spaBucket = new s3.Bucket(this, "PlaceholderSpaBucket", {
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
      });
    }

    const spaOrigin =
      origins.S3BucketOrigin.withOriginAccessControl(spaBucket);

    // Custom origin header carrying the X-Origin-Verify secret. Added only to
    // the API and chat origins (the SPA bucket is reached via OAC, not the
    // header). CloudFront requires this to be a plaintext string at deploy.
    const originVerifyHeaders: Record<string, string> = {
      "X-Origin-Verify": originVerifyValue,
    };

    // API origin: the API Gateway HTTP API (task 6.3). HttpOrigin because the
    // custom (REST/HTTP API) domain is a standard HTTPS endpoint.
    const apiOrigin = new origins.HttpOrigin(
      props.apiDomain ?? PLACEHOLDER_ORIGIN_DOMAIN,
      {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        customHeaders: originVerifyHeaders,
      }
    );

    // Chat origin: the Chat service Lambda Function URL (task 6.3). Also an
    // HTTPS endpoint; streaming responses pass through CloudFront unbuffered
    // when caching is disabled and the origin streams.
    const chatOrigin = new origins.HttpOrigin(
      props.chatFunctionUrlDomain ?? PLACEHOLDER_ORIGIN_DOMAIN,
      {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        customHeaders: originVerifyHeaders,
      }
    );

    // ------------------------------------------------------------------
    // Cache / origin-request policies for the dynamic (API) behaviors
    // ------------------------------------------------------------------
    // API traffic must never be cached and must forward the headers the
    // backend needs. Use the AWS managed policies to keep the template lean.
    const apiCachePolicy = cloudfront.CachePolicy.CACHING_DISABLED;

    // Forward all viewer headers except Host (Host must be the origin's), plus
    // all query strings. `ALL_VIEWER_EXCEPT_HOST_HEADER` also forwards the
    // `CloudFront-Viewer-Address` header that core.rate_limit reads for the
    // hashed client IP.
    const apiOriginRequestPolicy =
      cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER;

    // ------------------------------------------------------------------
    // CloudFront distribution
    // ------------------------------------------------------------------
    // Requirement 1.1 / 7.2: a single HTTPS public URL; the static SPA is
    // served from the CDN. Requirement 2.2: single CDN distribution + WAF.
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "ReviewLens AI public entry point (SPA + API + chat)",
      defaultRootObject: "index.html",
      // Attach the WAF WebACL (Requirement 2.2).
      webAclId: this.webAcl.attrArn,
      // HTTPS to viewers is enforced per-behavior via REDIRECT_TO_HTTPS below.
      // `minimumProtocolVersion` is intentionally omitted: it only takes effect
      // with a custom ACM certificate (a custom domain, not yet configured).
      // On the default *.cloudfront.net domain CloudFront fixes the TLS policy.

      // Default behavior: the SPA from S3. Cached, viewer redirected to HTTPS.
      defaultBehavior: {
        origin: spaOrigin,
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
      },

      additionalBehaviors: {
        // `/api/chat/*` must be matched BEFORE `/api/*`. CloudFront evaluates
        // the most specific path pattern first regardless of declaration
        // order, but we list it first for clarity. Chat streams responses, so
        // caching is disabled and all methods are allowed.
        "/api/chat/*": {
          origin: chatOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: apiCachePolicy,
          originRequestPolicy: apiOriginRequestPolicy,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          // Streaming: do not cache, and the origin (LWA + Function URL)
          // streams the body through CloudFront.
        },
        // `/api/*` → API Gateway HTTP API. No caching; forward headers + query.
        "/api/*": {
          origin: apiOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: apiCachePolicy,
          originRequestPolicy: apiOriginRequestPolicy,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        },
      },

      // SPA client-side routing: S3 returns 403 (OAC, key absent) or 404 for
      // deep links like /datasets/123. Rewrite both to index.html with 200 so
      // the React router can handle the path.
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(0),
        },
      ],
    });

    this.distributionDomainName = this.distribution.distributionDomainName;

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "DistributionDomainName", {
      value: this.distributionDomainName,
      description: "Public CloudFront domain for the Portal",
    });
    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
      description: "CloudFront distribution ID (for cache invalidation)",
    });
    new cdk.CfnOutput(this, "WebAclArn", {
      value: this.webAcl.attrArn,
      description: "WAF WebACL ARN attached to the distribution",
    });
  }
}
