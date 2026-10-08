/**
 * Stack stubs – each stack is implemented in platform-foundation task 6.
 *
 * Stacks to create:
 *   DataStack        – Aurora, S3, DynamoDB, Secrets Manager (task 6.1) ✅
 *   EdgeStack        – CloudFront + WAF (task 6.2) ✅
 *   ApiStack         – API Gateway, Lambda, EventBridge, SQS (task 6.3) ✅
 *   WorkersStack     – Worker/sweeper/push/DLQ Lambdas (task 6.4) ✅
 *   compute-mode     – ComputeMode seam: context value + ComputeTier (task 6.5) ✅
 *   RealtimeStack    – WebSocket API + EventBridge→push-queue rule
 *                      (dataset-library task 4.1; push consumer lives in Workers) ✅
 *   ContainersStack  – optional ECS Fargate compute, container mode (task 6.10) ✅
 *   FrontendStack    – S3 SPA origin (task 6.6) ✅
 *   CostStack        – AWS Budgets alarm (task 6.7) ✅
 *   TestFixturesStack – fixtures site (task 6.8) ✅
 *   GithubOidcStack  – OIDC deploy role (task 7.2) ✅
 */

export { DataStack } from "./data-stack";
export type { DataStackProps } from "./data-stack";

export { EdgeStack } from "./edge-stack";
export type { EdgeStackProps } from "./edge-stack";

export { ApiStack } from "./api-stack";
export type { ApiStackProps } from "./api-stack";

export { WorkersStack } from "./workers-stack";
export type { WorkersStackProps } from "./workers-stack";

export { RealtimeStack } from "./realtime-stack";
export type { RealtimeStackProps } from "./realtime-stack";

export { ContainersStack } from "./containers-stack";
export type { ContainersStackProps } from "./containers-stack";

export { FrontendStack } from "./frontend-stack";
export type { FrontendStackProps } from "./frontend-stack";

export { CostStack, DEFAULT_MONTHLY_BUDGET_USD } from "./cost-stack";
export type { CostStackProps } from "./cost-stack";

export { TestFixturesStack } from "./test-fixtures-stack";
export type { TestFixturesStackProps } from "./test-fixtures-stack";

export { GithubOidcStack } from "./github-oidc-stack";
export type { GithubOidcStackProps } from "./github-oidc-stack";

export {
  DEFAULT_COMPUTE_MODE,
  COMPUTE_MODES,
  parseComputeMode,
} from "./compute-mode";
export type { ComputeMode, ComputeTier } from "./compute-mode";
