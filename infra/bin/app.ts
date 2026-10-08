#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import {
  DataStack,
  EdgeStack,
  ApiStack,
  WorkersStack,
  RealtimeStack,
  ContainersStack,
  FrontendStack,
  CostStack,
  TestFixturesStack,
  GithubOidcStack,
  parseComputeMode,
} from "../lib/stacks";
import type { ComputeMode, ComputeTier } from "../lib/stacks";

const app = new cdk.App();

// ---------------------------------------------------------------------------
// Task 6.5: compute mode is a SINGLE, CENTRAL decision (Requirement 3.9).
//
// Read `computeMode` from context ONCE here and validate it. `lambda` is the
// default (and the only mode built today); `container` wires the optional
// ContainersStack (ECS Fargate, task 6.10). The switch lives entirely in
// infrastructure — no application code changes between modes, and the Lambda
// stacks are not rewritten. The Data/Edge/Frontend/Cost/TestFixtures stacks are
// compute-mode-agnostic and always deploy; only the COMPUTE tier differs.
const computeMode: ComputeMode = parseComputeMode(
  app.node.tryGetContext("computeMode")
);

// Standard CDK environment: account/region come from the CLI/CI credentials.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

// Allow throwaway environments (dev/CI) to tear down data resources cleanly.
// Production leaves this unset so buckets, tables, and the cluster are RETAINed.
const destroyableData =
  app.node.tryGetContext("destroyableData") === true ||
  app.node.tryGetContext("destroyableData") === "true";

// Task 6.1: the Data stack. Later tasks (6.2+) add the remaining stacks.
const data = new DataStack(app, "ReviewLens-Data", {
  env,
  destroyableData,
});

const envName =
  (app.node.tryGetContext("envName") as string | undefined) ?? "production";

// ---------------------------------------------------------------------------
// Compute tier — chosen by `computeMode` (task 6.5).
//
// Whichever branch runs produces a `ComputeTier` (the API + chat origin
// domains the Edge stack fronts). Both branches satisfy the same typed seam, so
// the Edge wiring below never branches on the compute mode.
let compute: ComputeTier;

if (computeMode === "lambda") {
  // -------------------------------------------------------------------------
  // Task 6.3: the Api stack (Lambda mode).
  //
  // The API/chat Lambdas, the EventBridge bus, and the SQS queues all live
  // here. It takes the DataStack so it can grant least-privilege access to
  // Aurora (Data API), S3, DynamoDB, and the secrets, and inject the matching
  // env vars. No Lambda joins a VPC — the database is reached over HTTPS
  // through the RDS Data API (Requirement 7.4), so there is no NAT gateway.
  const api = new ApiStack(app, "ReviewLens-Api", {
    env,
    data,
    envName,
  });

  // -------------------------------------------------------------------------
  // dataset-library task 4.1: the Realtime stack (WebSocket API).
  //
  // The WebSocket API with throttled stage, the $connect/$disconnect handlers
  // (which write/delete the ws-connections row), and the EventBridge rule that
  // routes dataset.status.changed and check.updated to the push-queue. It takes
  // the DataStack (ws-connections table) and the ApiStack (which owns the bus
  // and the push-queue). The push consumer that drains the queue lives in the
  // WorkersStack and is wired to this stack below. No Lambda joins a VPC
  // (Requirement 7.4).
  const realtime = new RealtimeStack(app, "ReviewLens-Realtime", {
    env,
    data,
    api,
    envName,
  });

  // -------------------------------------------------------------------------
  // Task 6.4: the Workers stack (Lambda mode).
  //
  // The asynchronous tier: job-worker Lambdas (workers image) consuming the
  // check and processing queues with partial-batch SQS event sources, the push
  // consumer, the DLQ backstop consumer, and the sweeper on an EventBridge
  // Scheduler schedule. It takes the DataStack (grants + env), the ApiStack
  // (which owns the queues, DLQs, and the EventBridge bus), and the
  // RealtimeStack (so the push consumer gets the WebSocket callback URL and a
  // scoped ManageConnections grant). As with the Api stack, no Lambda joins a
  // VPC — the database is reached over HTTPS through the RDS Data API
  // (Requirement 7.4), so there is no NAT gateway.
  new WorkersStack(app, "ReviewLens-Workers", {
    env,
    data,
    api,
    realtime,
    envName,
  });

  // ApiStack is the Lambda-mode ComputeTier (it implements the interface).
  compute = api;
} else {
  // -------------------------------------------------------------------------
  // Container mode — the optional ContainersStack (ECS Fargate, task 6.10).
  //
  // This is the documented seam for Requirement 3.9: a container mode can be
  // added per Service WITHOUT changing application code, by wiring a
  // ContainersStack here that satisfies the same `ComputeTier`. The Data, Edge,
  // Frontend, Cost, and TestFixtures stacks are unchanged.
  //
  // The ContainersStack (ECS Fargate, task 6.10) replaces BOTH the Api and
  // Workers stacks in container mode: it runs the same `backend`/`workers`
  // images as long-running Fargate services (API + chat behind an ALB, job
  // workers + push consumer as queue pollers, and the sweeper as a scheduled
  // Fargate task), and — since the Api stack is not created in this mode — it
  // also owns the EventBridge bus and the SQS queues. No application code
  // changes between modes; only the container command differs.
  const containers = new ContainersStack(app, "ReviewLens-Containers", {
    env,
    data,
    envName,
  });

  // ContainersStack implements ComputeTier (its ALB domain fronts both /api/*
  // and /api/chat/*). The Edge wiring below needs no change — it reads
  // `compute.apiDomain` and `compute.chatDomain` regardless of which stack
  // produced them.
  compute = containers;
}

// ---------------------------------------------------------------------------
// Task 6.2: the Edge stack (CloudFront + WAF).
//
// REGION: CloudFront WAF WebACLs (scope CLOUDFRONT) must live in us-east-1 and
// CloudFront is global, so the Edge stack is pinned to us-east-1 regardless of
// CDK_DEFAULT_REGION.
//
// ORIGINS: the API Gateway HTTP API (task 6.3), the Chat Function URL (task
// 6.3), and the SPA S3 bucket (task 6.6) are now wired below. EdgeStack still
// accepts each as an optional prop and falls back to safe placeholders so a
// standalone `cdk synth` of EdgeStack succeeds. The real wiring:
//
//   const api = new ApiStack(...);          // task 6.3
//   const frontend = new FrontendStack(...); // task 6.6
//   new EdgeStack(app, "ReviewLens-Edge", {
//     ...
//     apiDomain: api.httpApiDomain,
//     chatFunctionUrlDomain: api.chatFunctionUrlDomain,
//     spaBucket: frontend.bucket,
//   });
//
// ORIGIN-VERIFY SECRET: CloudFront custom origin headers must be plaintext in
// the template, so the value cannot be an unresolved Secrets Manager token.
// The deploy pipeline resolves the DataStack ORIGIN_VERIFY_SECRET value and
// passes it in via the `originVerifySecret` context key (e.g.
// `cdk deploy -c originVerifySecret="$(aws secretsmanager get-secret-value ...)"`),
// never hardcoded in source. When absent, EdgeStack uses a synth-only
// placeholder so `cdk synth` works; that placeholder must not be deployed.
const originVerifySecret = app.node.tryGetContext("originVerifySecret") as
  | string
  | undefined;

// CROSS-REGION WIRING: CloudFront custom-header origins must be plaintext
// hosts, and the Api stack's domains are CloudFormation tokens resolved at
// deploy. CDK can only pass a token from one stack to another when both are in
// the SAME environment (account + region); CloudFront behaviors do not need the
// origin to be in us-east-1, but the Edge *WebACL* (scope CLOUDFRONT) does.
//
// To make the Api → Edge domain wiring work at synth, deploy the Edge stack to
// the same region as the Api stack (the CLOUDFRONT WebACL still deploys fine
// from any region as long as the stack env resolves to us-east-1 for the ACL —
// here we honour an explicit `edgeRegion` context, defaulting to us-east-1).
//
// Wire the real Api domains only when the Edge stack shares the Api stack's
// region, so the tokens are resolvable. Otherwise fall back to the EdgeStack
// placeholders (synth still succeeds) and let the deploy pipeline pass the
// domains in as context strings.
const edgeRegion =
  (app.node.tryGetContext("edgeRegion") as string | undefined) ?? "us-east-1";
const sameRegion = env.region !== undefined && env.region === edgeRegion;

// ---------------------------------------------------------------------------
// Task 6.6: the Frontend stack (private S3 origin for the React SPA).
//
// Compute-mode-agnostic: always deploys, identically in Lambda and container
// mode. REGION: EdgeStack reads `frontend.bucket` as a cross-stack reference,
// which only resolves at synth when both stacks share an environment. EdgeStack
// is pinned to `edgeRegion` (us-east-1 for the CLOUDFRONT WebACL), so the
// Frontend stack is created in the SAME region. CloudFront OAC can reference a
// bucket in any region, but keeping them co-located lets the `spaBucket` token
// resolve cleanly with no cross-region export. The deploy pipeline (task 7.2)
// uploads the built SPA to this bucket.
const frontend = new FrontendStack(app, "ReviewLens-Frontend", {
  env: { account: env.account, region: edgeRegion },
  destroyableData,
});

new EdgeStack(app, "ReviewLens-Edge", {
  env: { account: env.account, region: edgeRegion },
  originVerifySecretValue: originVerifySecret,
  // Wire the active compute tier's origins when Edge shares its region so the
  // tokens resolve; otherwise the EdgeStack placeholders are used (see its
  // docs). `compute` is the ComputeTier produced by whichever compute mode ran,
  // so this wiring is identical in Lambda and (future) container mode.
  apiDomain: sameRegion
    ? compute.apiDomain
    : (app.node.tryGetContext("apiDomain") as string | undefined),
  chatFunctionUrlDomain: sameRegion
    ? compute.chatDomain
    : (app.node.tryGetContext("chatFunctionUrlDomain") as string | undefined),
  // Task 6.6: the real SPA bucket, replacing EdgeStack's private placeholder.
  // Both stacks share `edgeRegion`, so this cross-stack reference resolves at
  // synth. EdgeStack fronts it with Origin Access Control.
  spaBucket: frontend.bucket,
});

// ---------------------------------------------------------------------------
// Task 6.7: the Cost stack (AWS Budgets monthly alarm).
//
// Compute-mode-agnostic: always deploys, identically in Lambda and container
// mode. AWS Budgets is account-scoped (not regional), so this is defined ONCE
// per account; it is created in the standard CDK env for consistency.
//
// The alert email and budget amount come from context, never hardcoded:
//   cdk deploy -c budgetAlertEmail=ops@example.com -c monthlyBudgetUsd=50
// When no email is supplied the budget is still created, but with no
// notification/subscriber, so `cdk synth` succeeds without a placeholder.
//
// The Anthropic (AI provider) spend limit is set manually in the Anthropic
// console (no CDK resource exists) — a "needs a person" item documented in the
// README (Requirement 7.5).
const budgetAlertEmail = app.node.tryGetContext("budgetAlertEmail") as
  | string
  | undefined;
const monthlyBudgetUsdRaw = app.node.tryGetContext("monthlyBudgetUsd") as
  | string
  | number
  | undefined;
const monthlyBudgetUsd =
  monthlyBudgetUsdRaw !== undefined ? Number(monthlyBudgetUsdRaw) : undefined;

new CostStack(app, "ReviewLens-Cost", {
  env,
  alertEmail: budgetAlertEmail,
  monthlyBudgetUsd,
});

// ---------------------------------------------------------------------------
// Task 6.8: the Test fixtures stack (public S3 + CloudFront fixtures site).
//
// NON-PRODUCTION ONLY (Requirement 8.6). This is a test-only resource that
// publishes `/fixtures-site` to a public CloudFront URL so tests against a
// DEPLOYED stack (where SSRF protection blocks private addresses) can fetch
// fixture review pages over HTTPS. It MUST NOT exist in a production account,
// so it is instantiated ONLY when `envName !== "production"`.
//
// `envName` defaults to "production" (see above), so by default this stack is
// ABSENT from `cdk list` / `cdk synth`. Select a non-production environment to
// include it, e.g.:
//
//   cdk list -c envName=test        → includes ReviewLens-TestFixtures
//   cdk list                        → omits it (production default)
//
// The bucket is private (public access blocked) and fronted by CloudFront via
// OAC; the SITE is public through the CloudFront URL only. Compute-mode-agnostic
// like the Data/Edge/Frontend/Cost stacks.
if (envName !== "production") {
  new TestFixturesStack(app, "ReviewLens-TestFixtures", { env });
}

// ---------------------------------------------------------------------------
// Task 7.2: the GitHub OIDC stack (the deploy pipeline's trust anchor).
//
// DEPLOYED ONCE, BY HAND. `deploy.yml` assumes the role this stack creates, so
// the role must exist BEFORE the first automated deploy. A human deploys this
// stack manually (with their own credentials) and copies the exported role ARN
// into the GitHub repo variable AWS_DEPLOY_ROLE_ARN. The automated pipeline
// never deploys this stack — it only deploys the application stacks.
//
// It is ALWAYS present in the app so it synthesizes and type-checks in CI
// (`cdk list` includes ReviewLens-GithubOidc); that is harmless because synth
// only produces a template. The repository coordinates are NOT hardcoded —
// they come from context and fall back to obvious placeholders so synth works
// without them:
//
//   cdk deploy ReviewLens-GithubOidc -c githubOwner=my-org -c githubRepo=my-repo
//
// Set `-c createOidcProvider=false` if the account already has a GitHub Actions
// OIDC provider (only one is allowed per account), and `-c cdkQualifier=...` if
// the environment was bootstrapped with a non-default qualifier.
const githubOwner = app.node.tryGetContext("githubOwner") as string | undefined;
const githubRepo = app.node.tryGetContext("githubRepo") as string | undefined;
const createOidcProviderRaw = app.node.tryGetContext("createOidcProvider");
const createOidcProvider =
  createOidcProviderRaw === undefined
    ? undefined
    : createOidcProviderRaw !== false && createOidcProviderRaw !== "false";
const cdkQualifier = app.node.tryGetContext("cdkQualifier") as
  | string
  | undefined;

new GithubOidcStack(app, "ReviewLens-GithubOidc", {
  env,
  githubOwner,
  githubRepo,
  createOidcProvider,
  cdkQualifier,
});

app.synth();
