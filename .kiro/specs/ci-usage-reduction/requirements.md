# Requirements Document

## Introduction

GitHub Actions minutes are being consumed faster than desired. The `ci.yml`
pipeline runs a heavy 7-job sequence (including two Docker Compose stacks for
integration/scale and a non-blocking perf stack) and triggers on **push to every
branch and on pull_request**, so routine work runs the full pipeline twice and
even docs-only changes spin up containers. This spec revises the CI/CD triggers
and job structure to cut usage while keeping `main` fully gated before deploy and
every PR getting fast correctness signal. The chosen strategy is the aggressive
tier from the design: fast checks always run; heavy container/scale/synth/perf
jobs become opt-in for routine PRs.

## Glossary

- **Always-run tier:** the cheap jobs that run on every PR and push to main — lint/type check, unit/property tests, image build.
- **Heavy tier:** the expensive jobs — container integration tests, scale test, CDK synth, and the non-blocking perf test.
- **`full-ci` label:** a PR label that forces the heavy tier to run on a PR that does not target `main`.
- **Deploy gate:** `deploy.yml` triggering on a successful `CI` workflow run on `main`.

## Requirements

### Requirement 1: Eliminate duplicate and unnecessary runs

**User Story:** As a maintainer, I want CI to run once per meaningful change, so
that I stop paying for duplicate and work-in-progress runs.

#### Acceptance criteria
1. WHEN a commit is pushed to a branch that has an open pull request THEN the
   system SHALL run the CI workflow exactly once for that commit (not once for
   push and once for pull_request).
2. WHEN a commit is pushed to a branch with no open pull request THEN the system
   SHALL NOT run the CI workflow for that push.
3. WHEN a commit is pushed to `main` THEN the system SHALL run the full CI
   pipeline (all tiers).
4. WHEN a newer commit supersedes an in-flight run on the same ref THEN the
   system SHALL cancel the superseded run (concurrency with cancel-in-progress).

### Requirement 2: Always-run fast tier

**User Story:** As a contributor, I want quick correctness signal on every PR,
so that obvious breakages are caught cheaply and fast.

#### Acceptance criteria
1. WHEN any pull request runs CI THEN the system SHALL run lint + type check,
   unit/property tests (backend + frontend), and the container image build.
2. The always-run tier SHALL NOT bring up a Docker Compose stack, seed a
   database, or run scale/perf tests.
3. WHEN only documentation changes (markdown, `docs/**`, `sessions/**`,
   `.kiro/**`) THEN the system MAY run only lint and SHALL NOT run the
   container/integration/scale/perf jobs.

### Requirement 3: Opt-in heavy tier

**User Story:** As a maintainer, I want the expensive container/scale/synth/perf
jobs to run only where they add value, so that routine feature-branch PRs are
cheap.

#### Acceptance criteria
1. WHEN a push targets `main`, OR a pull request's base branch is `main`, OR a
   pull request carries the `full-ci` label THEN the system SHALL run the heavy
   tier (integration tests, scale test, CDK synth, perf test) subject to the
   relevant path filter.
2. WHEN none of those conditions hold THEN the system SHALL skip the heavy-tier
   jobs.
3. WHEN only `infra/**` changes THEN CDK synth SHALL be able to run WITHOUT the
   integration and scale jobs running first.
4. WHEN no `infra/**` change is present on a run THEN CDK synth SHALL be skipped.

### Requirement 4: Preserve the deploy gate and safety

**User Story:** As the product owner, I want production to stay protected, so
that reducing CI cost never ships untested code.

#### Acceptance criteria
1. The deploy workflow SHALL continue to trigger only on a successful `CI`
   `workflow_run` on `main`.
2. WHEN code reaches `main` (via merge) THEN the full pipeline (lint, unit,
   build, integration, scale, synth) SHALL have run on that `main` commit before
   deploy.
3. The change SHALL NOT alter test content, coverage thresholds, or the
   pass/fail criteria of any job — only *when* jobs run.
4. The CI workflow SHALL keep the name `CI` so `deploy.yml`'s `workflow_run`
   trigger continues to match.

### Requirement 5: Trim live-eval runs

**User Story:** As a maintainer, I want the live-model eval workflow not to
double-run, so that the most expensive runs are minimized.

#### Acceptance criteria
1. WHEN a change matching the eval path filters is pushed to a PR branch THEN
   `evals.yml` SHALL run at most once (not once per push and once per PR).
2. `evals.yml` SHALL remain off the deploy-gating chain and SHALL continue to
   skip cleanly when `ANTHROPIC_API_KEY` is absent.

### Requirement 6: Documentation

**User Story:** As a contributor, I want the new CI model documented, so that I
know when heavy jobs run and how to force them.

#### Acceptance criteria
1. The README CI/CD section SHALL describe the trigger model (PR + push-to-main),
   the always-run vs. opt-in tiers, and the `full-ci` label escape hatch.
