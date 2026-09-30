# Setting up ReviewLens AI in Kiro

This guide gets the specs into a Kiro project and walks through building them in order.

## 1. Install the tools

| Tool | Why | Check |
|---|---|---|
| [Kiro](https://kiro.dev) (sign in with your AWS Builder ID or other supported login) | The IDE that runs the specs | Kiro opens and shows the Kiro panel |
| Git + a GitHub account | Source control, CI | `git --version` |
| Docker Desktop (or Docker Engine + Compose v2) | Every service runs as a container locally | `docker compose version` |
| Python 3.12 and [uv](https://docs.astral.sh/uv/) | Backend | `python3.12 --version`, `uv --version` |
| Node.js 20+ | Frontend and CDK | `node --version` |
| AWS CLI v2 + AWS CDK v2 | Deployment (needed from `platform-foundation` task 6 on) | `aws --version`, `npx cdk --version` |

You'll also need, before the tasks that use them:

- an **Anthropic API key** (for recording AI fixtures and running the evaluation suites);
- an **AWS account** with a CLI profile, for deploying.

## 2. Create the repository

```bash
mkdir reviewlens-ai && cd reviewlens-ai
git init -b main
unzip ~/Downloads/reviewlens-kiro-specs.zip   # creates .kiro/ and docs/
git add . && git commit -m "Add Kiro specs, steering, and brief"
gh repo create reviewlens-ai --private --source . --push   # or create it on github.com and push
```

After unzipping you should have:

```
.kiro/
  specs/        7 spec folders, each with requirements.md, design.md, tasks.md, .config.kiro
  steering/     product.md, tech.md, structure.md, spec-workflow.md, testing.md
docs/
  brief.md      the original brief plus later decisions
  KIRO-SETUP.md this guide
```

## 3. Open it in Kiro and check what it picked up

1. **File → Open Folder** and choose `reviewlens-ai`.
2. Open the **Kiro** panel from the activity bar.
3. Under **Specs**, you should see seven specs. Open any `tasks.md` and check that each task shows a **Start task** link above it.
4. Under **Agent Steering**, you should see the five steering files. Don't use "Generate Steering Docs" — it would overwrite these with generic versions.

If a spec doesn't appear, or tasks don't show **Start task**:

- Open that spec's `tasks.md` and choose **Sync Files** (or ask in chat: `#spec:<name> refresh this spec`).
- If it still doesn't appear, delete that spec's `.config.kiro` and reopen the folder; Kiro recreates it.
- Tasks need the exact form `- [ ] 1. Title` with sub-tasks indented two spaces (`  - [ ] 1.1 Title`). The files already use it; keep it if you edit.

## 4. Optional: connect MCP servers

These give Kiro's agent current AWS documentation while it writes CDK and Lambda code. Create `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "aws-docs": {
      "command": "uvx",
      "args": ["awslabs.aws-documentation-mcp-server@latest"],
      "env": { "FASTMCP_LOG_LEVEL": "ERROR" },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Check it connected in the Kiro panel under **MCP Servers**.

## 5. Optional: add agent hooks

In the Kiro panel, **Agent Hooks → +**, describe the hook in plain language. Useful ones for this project:

- *"When a Python file under backend/app is saved, run ruff and the unit tests for that module."*
- *"When a file under backend/prompts is saved, remind me to run `make eval` before marking the task done."*
- *"When a task in any tasks.md is marked complete, run `make lint` and `make test`."*

## 6. Build the specs in order

Kiro runs each spec's tasks independently, so follow the order in `.kiro/steering/spec-workflow.md`:

1. `platform-foundation`
2. `review-extraction`
3. `dataset-ingestion`
4. `review-analysis`
5. `dataset-library`
6. `ingestion-summary`
7. `guardrailed-chat`

For each spec:

1. Read `requirements.md` and `design.md` first. Change anything you disagree with **before** running tasks. After edits, open `tasks.md` and choose **Sync Files** so tasks match.
2. Set the agent to **Supervised** mode for the first spec so you review each change. Switch to **Autopilot** once you trust the pattern.
3. Run tasks one at a time with **Start task**, or use **Run all tasks** (Kiro runs independent tasks in parallel waves). Optional tasks (`- [ ]*`) are skipped by Run all.
4. After each task: review the diff, run `make lint && make test`, and commit.
5. When the spec is done, run `make test-int` and commit before moving on.

Tasks that need you (listed in `spec-workflow.md`):

- `review-extraction` 9.3: pick real review sites, save pages, and check the labels.
- `review-extraction` 9.4 and `guardrailed-chat` 7.4 and 8: live-model runs. Put `ANTHROPIC_API_KEY` in your local `.env` (never commit it).
- `platform-foundation` 7.2: the first deployment.

## 7. Prepare AWS for the first deploy

Do this when you reach `platform-foundation` task 6 or 7.

1. Create a CLI profile: `aws configure --profile reviewlens`.
2. Bootstrap CDK once per account and region: `npx cdk bootstrap aws://<account>/<region> --profile reviewlens`.
3. Store the Anthropic key: `aws secretsmanager create-secret --name reviewlens/anthropic-api-key --secret-string '<key>' --profile reviewlens`.
4. Create the GitHub OIDC deploy role (task 7.2 adds a `GithubOidc` CDK stack; deploy that one stack once from your machine), then add the role ARN to the repo's GitHub Actions variables as `AWS_DEPLOY_ROLE_ARN`.
5. Set a spend limit in the Anthropic console, and confirm the AWS Budgets alarm email after the first deploy.

## 8. Keeping specs and code in step

- Change a requirement → edit `requirements.md`, ask Kiro to update `design.md`, then **Sync Files** on `tasks.md`.
- Found a gap while coding → add it to the spec first, then implement. This keeps the specs useful as documentation.
- To see progress across specs, ask in chat: *"#spec summarize which tasks are complete in each spec."*
