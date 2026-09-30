---
inclusion: always
---

# How to work through the specs

This project has seven specs in `.kiro/specs/`. They depend on each other, and Kiro runs each spec's tasks on its own, so **build them in this order** and finish (or deliberately defer) one before starting the next:

1. `platform-foundation` – scaffolding, images, core modules, consumer runtime, data layer, infrastructure, CI
2. `review-extraction` – the AI-first Extraction Engine and its evaluation suite
3. `dataset-ingestion` – URL checks, viability, Add, refresh service, uploads
4. `review-analysis` – the processing pipeline, sweeper, metrics
5. `dataset-library` – list, refresh, archive, real-time push
6. `ingestion-summary` – the detail page summary
7. `guardrailed-chat` – Q&A, scope guard, history, final E2E test

## Rules for the agent

- Before starting a task, read the requirements it references and the matching section of the spec's design.md.
- When a task uses something from an earlier spec (for example `refresh_service`, `extraction.build_plan`, `db.status.transition`), use the existing implementation. Don't re-implement it. If it's missing, stop and say which spec and task should provide it.
- Don't change another spec's files while working on a task; if a change is needed there, say so.
- Keep `tasks.md` checkboxes accurate. Mark a task done only when its tests pass (see tech.md).
- Tasks marked `- [ ]*` are optional and can be skipped for the first release.
- Tasks labeled **needs a person** or **needs `ANTHROPIC_API_KEY`** can't be finished unattended. Do the code parts, then stop and report what's left.

## Tasks that need a person

- `review-extraction` 9.3 – choose real review sites and hand-check the labels.
- `review-extraction` 9.4, `guardrailed-chat` 7.4 and 8 – live-model runs (API key and spend).
- `platform-foundation` 7.2 – the first deploy needs AWS credentials, a bootstrapped CDK environment, and the GitHub OIDC role.
