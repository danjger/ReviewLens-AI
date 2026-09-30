---
inclusion: fileMatch
fileMatchPattern: ["backend/tests/**", "frontend/src/**/*.test.ts", "frontend/src/**/*.test.tsx", "e2e/**", "evals/**"]
---

# Testing conventions

- **AI stub:** tests use `FakeClaude` from `tests/support/ai.py`, which replays recorded responses from `tests/fixtures/ai/`. Record new responses with `make record-ai` (needs `ANTHROPIC_API_KEY`); never hand-write them unless testing malformed output.
- **Fixture pages:** load review pages from `/fixtures-site` over HTTP (the Compose `fixtures` host is allowed by `SSRF_TEST_ALLOW_HOSTS`). Don't point tests at real websites.
- **Property tests:** live in `tests/property/`, use Hypothesis profiles `dev` (100 examples) and `ci` (500), and name the design property in the docstring.
- **Integration tests:** run against `make up`; each test creates its own dataset IDs and cleans up its S3 prefix and rows.
- **Scale tests:** start consumers with `docker compose up --scale` and assert on final database and S3 state, not on timing.
- **Frontend:** mock the API with MSW; mock the WebSocket with the helper in `src/test/ws.ts`.
- **E2E:** use data-testid selectors; never depend on text that includes counts or dates.
