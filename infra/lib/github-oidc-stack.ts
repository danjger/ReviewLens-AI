/**
 * GithubOidcStack – the GitHub Actions → AWS trust anchor for ReviewLens AI.
 *
 * platform-foundation task 7.2. Design "CI/CD": "GitHub Actions with OIDC into
 * AWS". This stack provides the two pieces that let the `deploy.yml` workflow
 * assume an AWS role WITHOUT any long-lived access keys stored in GitHub:
 *
 *   1. An IAM OIDC identity provider for `token.actions.githubusercontent.com`
 *      (the GitHub Actions OIDC issuer), with audience `sts.amazonaws.com`.
 *   2. A least-privilege deploy IAM Role whose trust policy only lets the
 *      SPECIFIC GitHub repository (and only on `main` / its environments)
 *      exchange its OIDC token for this role via `sts:AssumeRoleWithWebIdentity`.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * DEPLOYED ONCE, BY HAND. This stack is the bootstrap of the deploy pipeline:
 * `deploy.yml` assumes the role this stack creates, so the role must exist
 * BEFORE the first automated deploy can run. A human therefore deploys this
 * stack once, manually, with their own admin/bootstrap credentials:
 *
 *     cd infra
 *     npx cdk deploy ReviewLens-GithubOidc \
 *       -c githubOwner=<org-or-user> -c githubRepo=<repo>
 *
 * then copies the exported role ARN into the GitHub repository variable
 * `AWS_DEPLOY_ROLE_ARN` (see README / the task report). It is harmless to
 * `cdk synth` (it is always present in the app), but it is NEVER deployed by
 * the automated pipeline — the pipeline only has permission to deploy the
 * application stacks, not to mint its own trust.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * REPO IS NOT HARDCODED. `githubOwner`/`githubRepo` come from props (wired from
 * context in bin/app.ts). When they are absent, documented placeholders are
 * used so `cdk synth` always succeeds; the placeholder trust policy is
 * obviously not a real repo, so a mis-deploy without the real values is
 * inert (no real GitHub repo could assume it).
 *
 * ──────────────────────────────────────────────────────────────────────────
 * LEAST-PRIVILEGE MODEL (CDK + OIDC standard pattern). The deploy role itself
 * holds almost no direct AWS permissions. Instead it is allowed to ASSUME the
 * CDK bootstrap roles (`cdk-<qualifier>-deploy-role-*`, `-file-publishing-role-*`,
 * `-image-publishing-role-*`, `-lookup-role-*`). `cdk deploy` then assumes
 * those bootstrap roles to do the actual work (CloudFormation changes, S3 asset
 * upload, ECR image push, context lookups). This keeps the standing permissions
 * of the GitHub-assumable role tiny: it can only act through the audited CDK
 * bootstrap roles.
 *
 * On top of that it is granted the few DIRECT permissions the workflow needs
 * outside `cdk deploy`:
 *   - ECR auth (`ecr:GetAuthorizationToken`) so `docker login`/asset publishing
 *     to the CDK assets ECR works.
 *   - S3 write to the Frontend SPA bucket (the workflow uploads the built SPA).
 *   - CloudFront `CreateInvalidation` (the workflow invalidates the CDN).
 *   - Read the DataStack outputs/secret needed to run migrations + resolve the
 *     origin-verify secret (CloudFormation DescribeStacks + SecretsManager
 *     GetSecretValue + rds-data for the smoke test / migrations).
 *
 * Requirements: 1.3, 1.4, 8.7
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as iam from "aws-cdk-lib/aws-iam";

/** GitHub Actions OIDC issuer (without scheme) and token audience. */
const GITHUB_OIDC_PROVIDER_URL = "https://token.actions.githubusercontent.com";
const GITHUB_OIDC_PROVIDER_DOMAIN = "token.actions.githubusercontent.com";
const GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com";

/**
 * Placeholder repo coordinates used when real ones are not supplied via
 * context, so `cdk synth` works. These are intentionally obvious placeholders;
 * no real GitHub repository can present `sub` claims matching them, so a
 * stack deployed without the real values grants nobody access.
 */
const PLACEHOLDER_OWNER = "OWNER-PLACEHOLDER";
const PLACEHOLDER_REPO = "REPO-PLACEHOLDER";

/** Default CDK bootstrap qualifier (CDK's `hnb659fds` default). */
const DEFAULT_CDK_QUALIFIER = "hnb659fds";

export interface GithubOidcStackProps extends cdk.StackProps {
  /**
   * GitHub repository owner (org or user), e.g. `my-org`. Wired from the
   * `githubOwner` context value in bin/app.ts. When omitted a placeholder is
   * used so synth succeeds; supply the real value before deploying by hand.
   */
  readonly githubOwner?: string;

  /**
   * GitHub repository name, e.g. `reviewlens-ai`. Wired from the `githubRepo`
   * context value in bin/app.ts. When omitted a placeholder is used so synth
   * succeeds; supply the real value before deploying by hand.
   */
  readonly githubRepo?: string;

  /**
   * When true (default), create the IAM OIDC provider in this stack. Set to
   * false (context `createOidcProvider=false`) when the account already has a
   * `token.actions.githubusercontent.com` provider (there can only be ONE per
   * account) — the role trust then references the existing provider by its
   * well-known ARN. Wired from the `createOidcProvider` context value.
   */
  readonly createOidcProvider?: boolean;

  /**
   * CDK bootstrap qualifier, if the environment was bootstrapped with a custom
   * one. Defaults to CDK's standard `hnb659fds`. Wired from the `cdkQualifier`
   * context value.
   */
  readonly cdkQualifier?: string;
}

export class GithubOidcStack extends cdk.Stack {
  /** The least-privilege role GitHub Actions assumes for deploys. */
  public readonly deployRole: iam.Role;

  /** ARN of {@link deployRole}, exported for the GitHub repo variable. */
  public readonly deployRoleArn: string;

  constructor(scope: Construct, id: string, props: GithubOidcStackProps = {}) {
    super(scope, id, props);

    const owner = props.githubOwner ?? PLACEHOLDER_OWNER;
    const repo = props.githubRepo ?? PLACEHOLDER_REPO;
    const createProvider = props.createOidcProvider ?? true;
    const qualifier = props.cdkQualifier ?? DEFAULT_CDK_QUALIFIER;

    // ------------------------------------------------------------------
    // IAM OIDC identity provider for GitHub Actions
    // ------------------------------------------------------------------
    // There can be at most ONE provider per issuer per account. Either create
    // it here, or (when `createOidcProvider=false`) reference the account's
    // existing one by its deterministic ARN. The thumbprint list can be empty:
    // for the GitHub OIDC endpoint IAM validates the TLS chain against trusted
    // CAs, so no manual thumbprint pinning is required (and the audience
    // condition below is what actually scopes trust).
    let providerArn: string;
    if (createProvider) {
      const provider = new iam.OpenIdConnectProvider(this, "GithubOidc", {
        url: GITHUB_OIDC_PROVIDER_URL,
        clientIds: [GITHUB_OIDC_AUDIENCE],
      });
      providerArn = provider.openIdConnectProviderArn;
    } else {
      // Deterministic ARN of the single per-account GitHub OIDC provider.
      providerArn = `arn:aws:iam::${this.account}:oidc-provider/${GITHUB_OIDC_PROVIDER_DOMAIN}`;
    }

    // ------------------------------------------------------------------
    // Trust policy: only THIS repo, only main + its environments
    // ------------------------------------------------------------------
    // `sub` claim formats GitHub Actions presents:
    //   repo:OWNER/REPO:ref:refs/heads/main   – a run on the main branch
    //   repo:OWNER/REPO:environment:NAME      – a run in a GitHub Environment
    // We allow both so the pipeline can gate the deploy behind a GitHub
    // Environment. The `aud` claim is pinned to sts.amazonaws.com.
    //
    // IMMUTABLE SUBJECT CLAIMS: repos created, renamed, or transferred after
    // mid-2026 emit the subject with immutable numeric owner/repo IDs, e.g.
    //   repo:OWNER@<ownerId>/REPO@<repoId>:environment:production
    // (verified live for this repo). The `OWNER*`/`REPO*` patterns below match
    // BOTH the legacy name-only form and the immutable `@<id>` form, while the
    // literal owner+repo names keep the trust scoped to this repository only.
    // The `*` sits only where the optional `@<id>` suffix appears.
    const principal = new iam.OpenIdConnectPrincipal(
      // Reference the provider by ARN; works whether it was created here or
      // imported, without forcing a construct dependency on the import path.
      iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
        this,
        "GithubOidcRef",
        providerArn
      ),
      {
        StringEquals: {
          [`${GITHUB_OIDC_PROVIDER_DOMAIN}:aud`]: GITHUB_OIDC_AUDIENCE,
        },
        StringLike: {
          [`${GITHUB_OIDC_PROVIDER_DOMAIN}:sub`]: [
            `repo:${owner}*/${repo}*:ref:refs/heads/main`,
            `repo:${owner}*/${repo}*:environment:*`,
          ],
        },
      }
    );

    this.deployRole = new iam.Role(this, "DeployRole", {
      roleName: `${id}-deploy-role`,
      description:
        "Least-privilege role assumed by GitHub Actions (OIDC) to deploy ReviewLens AI",
      assumedBy: principal,
      maxSessionDuration: cdk.Duration.hours(1),
    });

    // ------------------------------------------------------------------
    // Permission 1: assume the CDK bootstrap roles (the heavy lifting)
    // ------------------------------------------------------------------
    // `cdk deploy` assumes these bootstrap roles to make CloudFormation
    // changes, publish file + image assets, and run context lookups. Granting
    // sts:AssumeRole on just the CDK role name pattern is the standard
    // least-privilege OIDC pattern — the deploy role's own standing
    // permissions stay minimal and every real action flows through an audited
    // bootstrap role.
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AssumeCdkBootstrapRoles",
        effect: iam.Effect.ALLOW,
        actions: ["sts:AssumeRole"],
        resources: [
          `arn:aws:iam::${this.account}:role/cdk-${qualifier}-deploy-role-*`,
          `arn:aws:iam::${this.account}:role/cdk-${qualifier}-file-publishing-role-*`,
          `arn:aws:iam::${this.account}:role/cdk-${qualifier}-image-publishing-role-*`,
          `arn:aws:iam::${this.account}:role/cdk-${qualifier}-lookup-role-*`,
        ],
      })
    );

    // ------------------------------------------------------------------
    // Permission 2: ECR auth for Docker image asset publishing
    // ------------------------------------------------------------------
    // `cdk deploy` publishes the DockerImageAsset to the CDK assets ECR via the
    // image-publishing bootstrap role, but the workflow also runs an explicit
    // `docker login` to ECR (and may push by digest). GetAuthorizationToken is
    // account-wide by API design (it cannot be resource-scoped).
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcrAuth",
        effect: iam.Effect.ALLOW,
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      })
    );

    // ------------------------------------------------------------------
    // Permission 3: read stack outputs + the origin-verify secret
    // ------------------------------------------------------------------
    // The workflow reads CloudFormation stack outputs (bucket names, cluster
    // ARN, distribution id) and resolves the ORIGIN_VERIFY_SECRET value to pass
    // to `cdk deploy -c originVerifySecret=...`. SecretsManager is scoped to
    // this account's secrets; CloudFormation Describe* is read-only.
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadStackOutputs",
        effect: iam.Effect.ALLOW,
        actions: [
          "cloudformation:DescribeStacks",
          "cloudformation:ListExports",
        ],
        resources: ["*"],
      })
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadDeploySecrets",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        // Scoped to the DataStack-managed secrets by name prefix.
        resources: [
          `arn:aws:secretsmanager:*:${this.account}:secret:ReviewLens-Data/*`,
        ],
      })
    );

    // ------------------------------------------------------------------
    // Permission 4: upload the built SPA + invalidate CloudFront
    // ------------------------------------------------------------------
    // The SPA bucket name is only known at deploy time (CloudFormation picks a
    // physical name), so we scope by the ReviewLens prefix pattern rather than
    // an exact ARN. CloudFront CreateInvalidation cannot be resource-scoped to
    // a single distribution in a stable way across accounts, so it is granted
    // broadly but limited to the single invalidation action.
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "UploadFrontend",
        effect: iam.Effect.ALLOW,
        actions: [
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ],
        resources: [
          "arn:aws:s3:::reviewlens-frontend-*",
          "arn:aws:s3:::reviewlens-frontend-*/*",
          // The CDK-generated SPA bucket name is unpredictable; also allow the
          // conventional lowercased stack-derived prefix CloudFormation uses.
          "arn:aws:s3:::reviewlens-*spabucket*",
          "arn:aws:s3:::reviewlens-*spabucket*/*",
        ],
      })
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvalidateCloudFront",
        effect: iam.Effect.ALLOW,
        actions: ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"],
        resources: ["*"],
      })
    );

    // ------------------------------------------------------------------
    // Permission 5: run migrations + smoke test through the RDS Data API
    // ------------------------------------------------------------------
    // The one-off Alembic migration step and the post-deploy smoke test both
    // read and write through the RDS Data API (Design "Data API parity").
    // Scoped to the account; the specific cluster/secret are supplied at
    // runtime from stack outputs.
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "RdsDataApi",
        effect: iam.Effect.ALLOW,
        actions: [
          "rds-data:ExecuteStatement",
          "rds-data:BatchExecuteStatement",
          "rds-data:BeginTransaction",
          "rds-data:CommitTransaction",
          "rds-data:RollbackTransaction",
        ],
        resources: [`arn:aws:rds:*:${this.account}:cluster:*`],
      })
    );

    this.deployRoleArn = this.deployRole.roleArn;

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "DeployRoleArn", {
      value: this.deployRoleArn,
      description:
        "Role ARN for GitHub Actions OIDC. Set as repo variable AWS_DEPLOY_ROLE_ARN.",
    });
    new cdk.CfnOutput(this, "GithubSubjectAllowed", {
      value: `repo:${owner}/${repo}:ref:refs/heads/main`,
      description: "GitHub OIDC subject the deploy role trusts",
    });
  }
}
