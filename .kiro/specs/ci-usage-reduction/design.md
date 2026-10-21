# Design: Reduce GitHub Actions usage

## Overview

Cut GitHub Actions minutes without losing the safety that matters: `main` stays
fully gated before deploy, and every PR still gets fast correctness signal.
Strategy chosen: **aggressive** — the balanced changes plus making the heavy
container jobs opt-in for routine PRs.

### Current state (what burns minutes)

Three workflows exist today:

- **`ci.yml`** — 7 jobs, chained `needs:` in order: `lint-and-typecheck →
  unit-tests → build-images → integration-tests → scale-test → cdk-synth`, plus
  a non-blocking `perf-test` that forks off `build-images`.
- **`deploy.yml`** — triggers on `workflow_run` of **CI** completing `success`
  on `main`. This is the deploy gate and must keep working.
- **`evals.yml`** — live-model quality gates, already path-filtered and already
  not on the deploy chain. Low usage; left essentially as-is.

Problems in `ci.yml`:

1. **Double runs.** Triggers are `push: branches: ["**"]` **and**
   `pull_request`. Every commit on a PR branch runs the whole pipeline twice.
2. **Runs on every branch push.** WIP pushes to any feature branch trigger the
   full 7-job pipeline even with no PR open.
3. **No path filtering.** A docs-only change spins up Docker Compose,
   integration, scale, and perf jobs for zero code change.
4. **Heavy jobs on every run.** `integration-tests`, `scale-test`, and
   `perf-test` each bring up a Compose stack (Postgres + LocalStack + fixtures),
   install deps, and build/pull images. These are the most expensive minutes.

## Architecture

The pipeline is reorganized around two trigger changes and a two-tier job model.

### Triggers

```yaml
on:
  push:
    branches: [main]      # was: ["**"]
  pull_request:           # all PRs (any base)
```

- A commit on a feature branch with an open PR runs CI **once** (the
  `pull_request` event), not twice.
- A push to a branch **without** a PR runs nothing.
- Pushes to `main` (merges) run the full pipeline, which gates `deploy.yml`.
- `concurrency` with `cancel-in-progress` stays, so superseded commits cancel.

### Two-tier job model

**Always-run tier** (every PR + every push to main): `lint-and-typecheck`,
`unit-tests`, `build-images`.

**Heavy tier** (opt-in): `integration-tests`, `scale-test`, `cdk-synth`,
`perf-test`. Runs only when:
- push to `main` (the deploy gate — must stay complete), OR
- a PR whose base is `main`, OR
- a PR labelled `full-ci`.

### Deploy gate unchanged

`deploy.yml` gates on a successful **CI** `workflow_run` on `main`. A push to
`main` keeps running all tiers, so `main` is still fully tested before deploy;
only feature-branch / non-main-PR runs get lighter. The workflow name stays `CI`.

## Components and Interfaces

### `changes` job (new)

A tiny job using `dorny/paths-filter` that classifies the diff and exposes
outputs the other jobs gate on:

- `backend` — anything under `backend/`.
- `frontend` — anything under `frontend/`.
- `infra` — anything under `infra/`.
- `docs_only` — only markdown / `docs/**` / `sessions/**` / `.kiro/**`.

### Per-job gating

- `lint-and-typecheck`: unconditional (cheap, catches most issues).
- `unit-tests`: always; frontend/backend sub-steps may key off `changes`.
- `build-images`: always (validates both Dockerfiles).
- `integration-tests` / `scale-test` / `perf-test`:
  `if: (ref==main OR base_ref==main OR label full-ci) AND changes.backend`.
- `cdk-synth`: decoupled from `scale-test` (`needs: [changes]`); runs only when
  `changes.infra` AND the heavy-tier condition hold.

### `evals.yml`

Narrow `push` to `branches: [main]` so eval runs are PR-triggered (or on main),
not doubled. Keep path filters, `workflow_dispatch`, and the skip-without-key
behavior.

## Data Models

No application data models change. The only structured artifacts are the GitHub
Actions workflow definitions:

- `ci.yml` — triggers (`on`), the `changes` job outputs
  (`backend`/`frontend`/`infra`/`docs_only`), and per-job `if` conditions.
- `evals.yml` — `on.push.branches`.
- `deploy.yml` — unchanged (`workflow_run: [CI]`).

The `full-ci` label is a repository PR label consumed via
`contains(github.event.pull_request.labels.*.name, 'full-ci')`.

## Error Handling

- **Path filter under-triggers** (a code change misclassified as docs-only):
  mitigated by conservative filters — anything under `backend/` is code; only
  doc/sessions/spec paths are docs-only.
- **Heavy jobs skipped on a feature PR**: by design. They still run before merge
  to `main` (via the main-targeting PR or the push-to-main run), so production
  stays protected; `full-ci` forces them early when needed.
- **YAML/job-graph errors**: validated by parsing all three workflow files and
  checking the job graph resolves before merge.

## Testing Strategy

- Parse/lint all three workflow YAML files; confirm the job graph resolves and
  the CI workflow name is still `CI`.
- Confirm `deploy.yml` still references `workflow_run: [CI]`.
- Spot-check expected behavior via the trigger/condition logic:
  - routine feature-branch PR → only lint + unit + build;
  - docs-only PR → lint only (no Docker);
  - PR targeting `main` or `full-ci`-labelled → full pipeline;
  - push to `main` → full pipeline → deploy gate.
- No change to test content or coverage thresholds — only *when* jobs run.

## Expected effect

- **Routine feature-branch PR (not targeting main):** lint + unit + build only,
  run **once** — largest, most common saving.
- **Docs-only PR:** lint (or nothing) — no Docker.
- **PR targeting `main` / labelled `full-ci`:** full pipeline (unchanged).
- **Push to `main`:** full pipeline → deploy gate unchanged.
- **Evals:** no longer double-run on PR branches.

## Files touched

- `.github/workflows/ci.yml` — triggers, a `changes` job, per-job `if` gates,
  decoupled `cdk-synth`.
- `.github/workflows/evals.yml` — `push` branches narrowed to `main`.
- `.github/workflows/deploy.yml` — unchanged (verify the gate still fires).
- `README.md` (CI/CD section) — document the new trigger model and the
  `full-ci` label.
