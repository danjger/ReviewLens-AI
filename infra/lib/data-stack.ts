/**
 * DataStack – the shared persistence layer for ReviewLens AI.
 *
 * platform-foundation task 6.1. Provides:
 *   - Aurora PostgreSQL Serverless v2 cluster (min 0 ACU auto-pause, RDS Data
 *     API enabled). Compute never joins the VPC: it reaches the database over
 *     HTTPS through the Data API, so there is no NAT gateway.
 *   - A private S3 bucket (public access blocked, SSE-S3, SSL enforced) with
 *     lifecycle rules that delete `checks/` and `uploads/` objects after 1 day.
 *   - DynamoDB tables on-demand: `rate-limits`, `check-sessions`,
 *     `ws-connections`, with TTL where short-lived state is kept.
 *   - Secrets Manager entries: the Aurora-generated credentials secret plus
 *     placeholder secrets for the Anthropic API key and the X-Origin-Verify
 *     secret (populated out-of-band; never hardcoded here).
 *
 * Requirements: 4.1, 5.1, 5.2, 5.3, 6.1, 7.1, 7.4
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as rds from "aws-cdk-lib/aws-rds";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";

export interface DataStackProps extends cdk.StackProps {
  /**
   * When true, resources use retention-friendly removal policies so a stack
   * teardown in a throwaway environment does not block on non-empty buckets or
   * orphaned snapshots. Defaults to false (RETAIN) to protect real data.
   */
  readonly destroyableData?: boolean;
}

export class DataStack extends cdk.Stack {
  /** Aurora Serverless v2 cluster, reached only through the Data API. */
  public readonly cluster: rds.DatabaseCluster;
  /** ARN of the cluster, consumed by compute stacks for Data API calls. */
  public readonly clusterArn: string;
  /** ARN of the Aurora-generated credentials secret. */
  public readonly dbSecretArn: string;
  /** Logical database name created inside the cluster. */
  public readonly databaseName: string;

  /** Private application bucket (datasets/, checks/, uploads/). */
  public readonly bucket: s3.Bucket;

  /** DynamoDB tables. */
  public readonly rateLimitsTable: dynamodb.Table;
  public readonly checkSessionsTable: dynamodb.Table;
  public readonly wsConnectionsTable: dynamodb.Table;

  /** Placeholder secret for the Anthropic API key (populated out-of-band). */
  public readonly anthropicSecret: secretsmanager.Secret;
  /** Secret for the X-Origin-Verify header shared with CloudFront. */
  public readonly originVerifySecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: DataStackProps = {}) {
    super(scope, id, props);

    const destroyable = props.destroyableData ?? false;
    const removalPolicy = destroyable
      ? cdk.RemovalPolicy.DESTROY
      : cdk.RemovalPolicy.RETAIN;

    this.databaseName = "reviewlens";

    // ------------------------------------------------------------------
    // Networking
    // ------------------------------------------------------------------
    // Aurora requires a VPC, but no application compute runs inside it and the
    // Data API is reached over HTTPS. We therefore create a VPC with ONLY
    // isolated subnets: no public subnets, no NAT gateways, no internet
    // gateway cost. This satisfies "no NAT gateway" (Requirement 7.4).
    const vpc = new ec2.Vpc(this, "DataVpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "db-isolated",
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    // ------------------------------------------------------------------
    // Aurora PostgreSQL Serverless v2 (Data API)
    // ------------------------------------------------------------------
    this.cluster = new rds.DatabaseCluster(this, "Aurora", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        // 16.4 was removed from Aurora PostgreSQL availability; pin to
        // 16.8 (lowest still-available 16.x in us-east-1 as of deploy).
        version: rds.AuroraPostgresEngineVersion.VER_16_8,
      }),
      // Serverless v2 scaling: min 0 ACU allows the cluster to auto-pause when
      // idle (near-zero idle cost, Requirement 7.1). First request after a
      // pause takes ~15s while the database resumes.
      serverlessV2MinCapacity: 0,
      serverlessV2MaxCapacity: 4,
      writer: rds.ClusterInstance.serverlessV2("Writer"),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      // RDS Data API: lets stateless compute query the DB over HTTPS with no
      // VPC attachment and no connection pool to exhaust (Requirement 4.1).
      enableDataApi: true,
      // Aurora generates and rotates the admin credentials in Secrets Manager
      // (Requirement 6.1). The secret name is not hardcoded with a value.
      credentials: rds.Credentials.fromGeneratedSecret("reviewlens_admin", {
        secretName: `${id}/aurora/credentials`,
      }),
      defaultDatabaseName: this.databaseName,
      storageEncrypted: true,
      removalPolicy,
    });

    this.clusterArn = this.cluster.clusterArn;
    // The generated secret is always present when credentials.fromGeneratedSecret
    // is used, but the type is nullable; guard it explicitly.
    if (!this.cluster.secret) {
      throw new Error("Aurora cluster did not produce a credentials secret");
    }
    this.dbSecretArn = this.cluster.secret.secretArn;

    // ------------------------------------------------------------------
    // Private application S3 bucket
    // ------------------------------------------------------------------
    this.bucket = new s3.Bucket(this, "AppBucket", {
      // Requirement 5.2: block all public access.
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      // Requirement 5.3: encrypt objects at rest. SSE-S3 per design.
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy,
      autoDeleteObjects: destroyable,
      lifecycleRules: [
        // Requirement 5.1: temporary check captures and pending uploads are
        // deleted automatically after 1 day. One rule per temp prefix.
        {
          id: "expire-checks",
          prefix: "checks/",
          expiration: cdk.Duration.days(1),
        },
        {
          id: "expire-uploads",
          prefix: "uploads/",
          expiration: cdk.Duration.days(1),
        },
        // Housekeeping: abort incomplete multipart uploads so partial objects
        // do not accumulate cost. Does not affect permanent datasets/ objects.
        {
          id: "abort-incomplete-multipart",
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
    });

    // ------------------------------------------------------------------
    // DynamoDB tables (on-demand / PAY_PER_REQUEST)
    // ------------------------------------------------------------------
    // rate-limits: fixed-window counters. core.rate_limit keys items by a
    // string partition key `{action}#{scope}` on attribute `PK`, and uses an
    // epoch-seconds `ttl` attribute for automatic cleanup.
    this.rateLimitsTable = new dynamodb.Table(this, "RateLimitsTable", {
      tableName: "rate-limits",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy,
    });

    // check-sessions: short-lived per-check state (one item per check item).
    // Keyed by check_id with a sort key for each item; TTL expires sessions.
    this.checkSessionsTable = new dynamodb.Table(this, "CheckSessionsTable", {
      tableName: "check-sessions",
      partitionKey: { name: "check_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "item_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy,
    });

    // ws-connections: active WebSocket connection IDs for real-time push.
    // Keyed by connection_id; TTL reaps connections that were not cleaned up
    // by a disconnect event.
    this.wsConnectionsTable = new dynamodb.Table(this, "WsConnectionsTable", {
      tableName: "ws-connections",
      partitionKey: {
        name: "connection_id",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy,
    });

    // ------------------------------------------------------------------
    // Secrets Manager entries
    // ------------------------------------------------------------------
    // Placeholder for the Anthropic API key. Generated with a dummy value so
    // the secret exists; the real key is written out-of-band (console/CLI).
    // The JSON key is ANTHROPIC_API_KEY so core.config can merge it directly.
    this.anthropicSecret = new secretsmanager.Secret(this, "AnthropicApiKey", {
      secretName: `${id}/anthropic-api-key`,
      description:
        "Anthropic API key (ANTHROPIC_API_KEY). Populated out-of-band; placeholder here.",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ ANTHROPIC_API_KEY: "" }),
        generateStringKey: "placeholder",
      },
    });

    // X-Origin-Verify shared secret between CloudFront and the HTTP services.
    // Generated so a strong value exists from day one; the Edge stack reads it.
    this.originVerifySecret = new secretsmanager.Secret(
      this,
      "OriginVerifySecret",
      {
        secretName: `${id}/origin-verify-secret`,
        description:
          "Shared secret for the X-Origin-Verify header (ORIGIN_VERIFY_SECRET).",
        generateSecretString: {
          secretStringTemplate: JSON.stringify({}),
          generateStringKey: "ORIGIN_VERIFY_SECRET",
          excludePunctuation: true,
          passwordLength: 48,
        },
      }
    );

    // ------------------------------------------------------------------
    // Outputs (consumed by other stacks and the deploy smoke test)
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "ClusterArn", {
      value: this.clusterArn,
      description: "Aurora cluster ARN for RDS Data API calls",
    });
    new cdk.CfnOutput(this, "DbSecretArn", {
      value: this.dbSecretArn,
      description: "Aurora credentials secret ARN",
    });
    new cdk.CfnOutput(this, "DatabaseName", {
      value: this.databaseName,
      description: "Logical database name",
    });
    new cdk.CfnOutput(this, "BucketName", {
      value: this.bucket.bucketName,
      description: "Application S3 bucket name",
    });
    new cdk.CfnOutput(this, "RateLimitsTableName", {
      value: this.rateLimitsTable.tableName,
    });
    new cdk.CfnOutput(this, "CheckSessionsTableName", {
      value: this.checkSessionsTable.tableName,
    });
    new cdk.CfnOutput(this, "WsConnectionsTableName", {
      value: this.wsConnectionsTable.tableName,
    });
    new cdk.CfnOutput(this, "AnthropicSecretArn", {
      value: this.anthropicSecret.secretArn,
    });
    new cdk.CfnOutput(this, "OriginVerifySecretArn", {
      value: this.originVerifySecret.secretArn,
    });
  }
}
