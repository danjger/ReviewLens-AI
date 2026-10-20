# Implementation Plan

## Overview

Revise the CI/CD workflows to cut GitHub Actions usage: fix triggers to stop
duplicate and work-in-progress runs, split jobs into an always-run fast tier
and an opt-in heavy tier, add path filtering, and narrow the eval workflow's
push trigger — while keeping `main` fully gated before deploy.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "6", "7"] },
    { "wave": 2, "tasks": ["3", "4", "5"] },
    { "wave": 3, "tasks": ["8"] }
  ],
  "dependencies": {
    "1": [],
    "2": [],
    "3": ["2"],
    "4": ["2"],
    "5": ["2"],
    "6": [],
    "7": [],
    "8": ["3", "4", "5", "6", "7"]
  }
}
```

- Tasks 1 and 2 are the foundation; 3, 4, 5 depend on 2's `changes` outputs.
- Task 6 (evals.yml) and Task 7 (deploy gate verification) are independent.
- Task 8 (validate all YAML + README) is last.

## Tasks

- [x] 1. Revise `ci.yml` triggers to stop duplicate and WIP runs
  - Change `on.push.branches` from `["**"]` to `[main]`; keep `pull_request`
  - Keep the existing `concurrency` block (cancel-in-progress)
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 4.4_

- [x] 2. Add a `changes` job that classifies the diff
  - Use `dorny/paths-filter` to set outputs: `backend`, `frontend`, `infra`,
    and `docs_only`
  - Treat anything under `backend/` as code; `infra/**` as infra; markdown /
    `docs/**` / `sessions/**` / `.kiro/**` as docs
  - _Requirements: 2.3, 3.3, 3.4_

- [x] 3. Keep the always-run fast tier unconditional (except path skips)
  - `lint-and-typecheck`, `unit-tests`, `build-images` run on every PR and on
    push to main
  - Gate frontend-only vs backend-only test steps on the `changes` outputs where
    cheap to do so; keep lint unconditional
  - _Requirements: 2.1, 2.2_

- [x] 4. Gate the heavy tier as opt-in
  - Add an `if:` to `integration-tests`, `scale-test`, and `perf-test`:
    run when `github.ref == 'refs/heads/main'` OR `github.base_ref == 'main'`
    OR the PR has the `full-ci` label; AND the `backend` path filter is true
  - _Requirements: 3.1, 3.2_

- [x] 5. Decouple and conditionally run `cdk-synth`
  - Remove `needs: scale-test`; depend only on `changes` (and `build-images` if
    an image is needed — it is not for synth)
  - Run only when `infra` changed AND the heavy-tier condition holds
  - _Requirements: 3.3, 3.4_

- [x] 6. Narrow `evals.yml` push trigger
  - Change its `push` to `branches: [main]`; keep the path filters and
    `workflow_dispatch`
  - _Requirements: 5.1, 5.2_

- [x] 7. Verify the deploy gate is unaffected
  - Confirm `deploy.yml` still triggers on `workflow_run: [CI]` success on main
  - Confirm the CI workflow name stays `CI`
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 8. Validate all workflow YAML and document the model
  - Lint/parse all three workflow files (YAML valid; job graph resolves)
  - Update the README CI/CD section: trigger model, always-run vs opt-in tiers,
    and the `full-ci` label
  - _Requirements: 6.1_

## Notes

- No test content, coverage thresholds, or job pass/fail criteria change —
  only *when* jobs run.
- The CI workflow name MUST stay `CI` so `deploy.yml`'s `workflow_run`
  trigger keeps matching.
- The `full-ci` PR label is the escape hatch to run the heavy tier on a PR
  that does not target `main`.
