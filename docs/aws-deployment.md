# AWS deployment runbook

How to deploy ReviewLens AI to AWS and provide the Anthropic API key. This is
the `platform-foundation` task 7.2 "needs a person" item: a one-time manual
bootstrap, after which every push to `main` deploys automatically.

The infrastructure is AWS CDK (TypeScript) under `/infra`. Model: a human sets
up the trust anchor and secrets once by hand; thereafter `deploy.yml` assumes a
least-privilege OIDC role and deploys on every green CI run on `main`.

> Source of truth: `infra/bin/app.ts`, `infra/lib/github-oidc-stack.ts`,
> `infra/lib/data-stack.ts`, and `.github/workflows/deploy.yml`. If those change,
> update this runbook.

---

## Stacks (what gets deployed)

Defined in `infra/bin/app.ts`:

| Stack | Region | Deployed by | Notes |
|---|---|---|---|
| `ReviewLens-GithubOidc` | default | **human, once** | OIDC provider + deploy role. The pipeline never deploys this. |
| `ReviewLens-Data` | default | pipeline | Aurora (Data API), S3, DynamoDB, Secrets Manager (Anthropic key + origin-verify). |
| `ReviewLens-Api` | default | pipeline | API/chat Lambdas, SQS queues, EventBridge bus. |
| `ReviewLens-Workers` | default | pipeline | Check/processing workers, push consumer, sweeper. |
| `ReviewLens-Frontend` | `edgeRegion` (us-east-1) | pipeline | Private S3 bucket for the SPA. |
| `ReviewLens-Edge` | us-east-1 | pipeline | CloudFront + WAF (CLOUDFRONT WebACL must be us-east-1). |
| `ReviewLens-Cost` | default | pipeline | AWS Budgets monthly alarm. |
| `ReviewLens-TestFixtures` | default | pipeline (non-prod only) | Only when `envName != production`. |

"default" region = `CDK_DEFAULT_REGION` from the deploying credentials.

---

## One-time manual bootstrap

Do these once per AWS account with your own admin/bootstrap credentials.

### 0. Prerequisites

```bash
aws sts get-caller-identity          # confirm the right account
cd infra && npm ci
```

### 1. Bootstrap CDK

Modern CDK bootstrap, once per account/region. The Edge/Frontend stacks pin to
`us-east-1`; if your default region differs, bootstrap both.

```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/<DEFAULT_REGION>
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1   # if default region != us-east-1
```

If you bootstrapped with a non-default qualifier, remember it for `-c cdkQualifier=...` below.

### 2. Deploy the GitHub OIDC trust anchor (by hand)

This creates the role `deploy.yml` assumes. Supply your real repo coordinates
(not hardcoded in source):

```bash
npx cdk deploy ReviewLens-GithubOidc \
  -c githubOwner=<org-or-user> \
  -c githubRepo=<repo>
# add -c createOidcProvider=false if the account already has a
#   token.actions.githubusercontent.com provider (only one allowed per account)
# add -c cdkQualifier=<qualifier> if you bootstrapped with a custom qualifier
```

Copy the `DeployRoleArn` output into the GitHub **repository variable**
`AWS_DEPLOY_ROLE_ARN` (Settings → Secrets and variables → Actions → Variables).
The deploy workflow reads `vars.AWS_DEPLOY_ROLE_ARN`. Optionally set the
`AWS_REGION` repo variable (defaults to `us-east-1`).

The trust policy only allows `repo:<owner>/<repo>:ref:refs/heads/main` and
`repo:<owner>/<repo>:environment:*` to assume the role — no long-lived keys.

### 3. First deploy of the Data stack (to create the secrets)

The Anthropic and origin-verify secrets live in `ReviewLens-Data`, so deploy it
before populating them or deploying Edge (Edge needs the origin-verify value as
plaintext context):

```bash
npx cdk deploy ReviewLens-Data
```

### 4. Populate the Anthropic API key

`ReviewLens-Data` creates the secret `ReviewLens-Data/anthropic-api-key` with a
placeholder body `{"ANTHROPIC_API_KEY": ""}`. The real value is written
out-of-band — never in source or the template. The secret body MUST stay JSON
with the `ANTHROPIC_API_KEY` key, because `app/core/config.py` merges the
secret's JSON keys into the environment at startup, and `app/core/ai.py` reads
`ANTHROPIC_API_KEY` from the environment.

```bash
aws secretsmanager put-secret-value \
  --secret-id ReviewLens-Data/anthropic-api-key \
  --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-...your-real-key..."}'
```

Verify:

```bash
aws secretsmanager get-secret-value \
  --secret-id ReviewLens-Data/anthropic-api-key \
  --query SecretString --output text
```

Lambdas/workers pick up the new value on their next cold start — no redeploy
needed (the ARN they read via `SECRETS_ARN` never changes). Confirm the exact
secret name with `aws secretsmanager list-secrets` or the `AnthropicSecretArn`
output of `ReviewLens-Data` if your stack id differs.

### 5. Set the Anthropic spend limit (needs a person)

There is no CDK resource for this (by design). Set a monthly spend limit in the
Anthropic console (console.anthropic.com) so a live run cannot overspend. This
is separate from the AWS Budgets alarm in `ReviewLens-Cost`.

### 6. (Optional) deploy the rest by hand once

The pipeline will deploy everything on the next push to `main`, but to bring the
stack up immediately, deploy the application stacks explicitly. Edge needs the
origin-verify secret value passed as context (it can't be an unresolved token in
a CloudFront custom header):

```bash
OVS=$(aws secretsmanager get-secret-value \
  --secret-id ReviewLens-Data/origin-verify-secret \
  --query SecretString --output text \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["ORIGIN_VERIFY_SECRET"])')

npx cdk deploy \
  ReviewLens-Data ReviewLens-Api ReviewLens-Workers \
  ReviewLens-Frontend ReviewLens-Edge ReviewLens-Cost \
  --require-approval never \
  -c envName=production \
  -c originVerifySecret="$OVS"
```

Then run migrations once (the pipeline does this automatically on each deploy):

```bash
cd ../backend
ENV=production \
DB_RESOURCE_ARN=<ReviewLens-Data ClusterArn> \
DB_SECRET_ARN=<ReviewLens-Data DbSecretArn> \
DB_DATABASE_NAME=<ReviewLens-Data DatabaseName> \
uv run alembic -c app/db/migrations/alembic.ini upgrade head
```

---

## Steady state: automatic deploys

Once the bootstrap above is done, no manual AWS steps are needed per release.
`.github/workflows/deploy.yml`:

1. Triggers on `workflow_run` of the `CI` workflow completing **successfully** on
   `main` (CI is the gate: lint, unit/property, integration, scale, cdk synth).
2. Assumes `AWS_DEPLOY_ROLE_ARN` via OIDC (`id-token: write`), logs in to ECR.
3. Resolves the origin-verify secret value.
4. `cdk deploy` of the six application stacks (explicitly, NOT `--all`, so
   `ReviewLens-GithubOidc` is never touched) — builds the backend/workers image
   assets by digest.
5. Runs Alembic migrations against the deployed DB via the Data API.
6. Builds + uploads the SPA to the Frontend bucket, invalidates CloudFront.
7. Runs the Lambda-mode smoke test.

The deployed portal URL and API endpoint are written to the job summary.

---

## How the Anthropic key reaches the running code

1. `ReviewLens-Data` creates the secret and exports `AnthropicSecretArn`.
2. `ReviewLens-Api` and `ReviewLens-Workers` set `SECRETS_ARN` = that ARN on
   every function and call `anthropicSecret.grantRead(fn)` (least privilege).
3. On startup, `app/core/config.py` sees `SECRETS_ARN`, fetches the secret, and
   merges its JSON keys into `os.environ` (only keys not already set).
4. `app/core/ai.py` constructs `anthropic.Anthropic()`, which reads
   `ANTHROPIC_API_KEY` from the environment. Model IDs come from config
   (`CLAUDE_*_MODEL`), never literals.

So "providing the key in AWS" = writing the real value into the
`ReviewLens-Data/anthropic-api-key` secret (step 4 above). Everything else is
already wired by the CDK.

---

## Where the key lives, by environment

| Environment | Where the key comes from |
|---|---|
| Local (`make up`, `make eval`, `make record-ai`) | `.env` at the repo root (gitignored); Compose services use `env_file: .env`. |
| GitHub Actions (the `evals.yml` live eval, Task 9.4) | Actions **secret** `ANTHROPIC_API_KEY`. |
| AWS (deployed app) | Secrets Manager secret `ReviewLens-Data/anthropic-api-key`, merged into env at startup. |

Never commit the key. Rotate by writing a new value to the secret / updating the
`.env` / Actions secret as appropriate.
