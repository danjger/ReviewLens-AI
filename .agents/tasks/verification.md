# Verification — ingestion-summary E2E scenario 1 rating fixture

Branch: `fix/ingestion-summary-e2e-rating-fixture`
Only file edited: `e2e/tests/ingestion-summary.spec.ts`

## Change made

- `uniqueFixtureUrl(tag)` gained an optional `path` parameter defaulting to
  `/extraction/plain_list/`; the URL is now built from `path` with the per-run
  nonce query string preserved exactly as before.
- Scenario 1 ("open a processed dataset: header, metrics, snapshot,
  completeness, filtered reviews") now seeds from the rating-cue-bearing fixture
  `/extraction/structured_jsonld/` (ratings 5/4/2, star cue spans + JSON-LD).
- Scenario 2 ("detail page moves from processing to ready live …") is UNTOUCHED:
  it still calls `uniqueFixtureUrl("live")` with no path argument, so it keeps
  using `/extraction/plain_list/`.

No backend/product/fixture files were changed. No product rule
(`_has_rating_cue`, viability thresholds, SSRF) was weakened.

## Commands run and results (all in this environment)

1. `cd e2e && npx tsc --noEmit` — PASS (exit 0, no errors). This is the binding
   TypeScript check for the edited file; `make lint`'s frontend leg does not
   cover the `e2e/` project.

2. `make lint` (repo root) — PASS (exit 0):
   - `ruff check .` → All checks passed!
   - `ruff format --check .` → 283 files already formatted
   - `mypy app/` → Success: no issues found in 97 source files
   - frontend `eslint . --max-warnings 0` → clean
   - frontend `tsc -b` → clean

3. Live stub E2E run (Docker available: Docker 29.8.1, Compose v5.5.1):
   - `make up` — full Compose stack started; all containers healthy
     (postgres, localstack, fixtures, api, chat, workers-check,
     workers-processing, push-consumer, sweeper). Compose sets `AI_STUB=1` and
     `SSRF_TEST_ALLOW_HOSTS=fixtures`.
   - Migrations applied from the host against the published Postgres port
     (the compose stack has no auto-migrate on startup, and `make` has no
     migrate target):
     `DATABASE_URL="postgresql+psycopg://reviewlens:reviewlens@localhost:5432/reviewlens" uv run alembic -c app/db/migrations/alembic.ini upgrade head`
     → `alembic current` reports `0002_html_upload_source_type (head)`.
   - API reachable: `GET http://localhost:8000/api/datasets?archived=false` → 200.
   - `cd e2e && npx playwright install chromium` then
     `cd e2e && npx playwright test ingestion-summary.spec.ts` (E2E_LIVE_AI NOT
     set) → **2 passed (12.0s)**:
       - scenario 1 PASSED (6.0s)
       - scenario 2 PASSED (4.1s)
   - Confirmed via `--reporter=json` that BOTH tests have status `expected` with
     result `passed` — neither skipped (no annotate-and-skip; the seed reached
     an active version and the detail page + rating filter were exercised).

## Addable-verdict / process confirmation under the stub (static + empirical)

Confirmed `/extraction/structured_jsonld/` reaches an addable verdict and
processes to `active_version` under the AI stub, exactly as the test's
`seedDataset` / `waitForActiveVersion` helpers expect:

- Empirically: scenario 1 passed end to end, which requires the Check verdict to
  be `will_work`/`limited` (seedDataset returns a dataset id) AND the dataset to
  reach an `active_version` (waitForActiveVersion returns true) AND the detail
  page to render metrics/snapshot/completeness/reviews.
- Statically: `app/core/ai_stub.ScriptedStubClient._locator_from_user` marks
  every `<p>` with text ≥ 40 chars as a review. The structured_jsonld page's
  three `<p class="review-body">` bodies are all ≥ 40 chars, so `has_reviews`
  is true → addable (`limited`) verdict, and three reviews extract → review
  count available (`metric-review-count data-available="true"`).

## Known limitation of the stub (documented in plan.md, not a defect of this fix)

Under `AI_STUB=1` the scripted stub supplies `rating_value=None` and no
`rating_ref` on every located item, so `postprocess._resolve_rating` returns
`None` and ratings 5/4/2 never reach the output. The rating=5 filter therefore
matches ZERO rows under the stub, so scenario 1's reviews body settles on the
`reviews-empty` branch and the "every rendered row matches 5" loop is vacuously
satisfied — the existing `rows > 0 || empty > 0` poll accepts this and the test
PASSES. The fix is correct and does not false-fail; the rating filter is only
genuinely exercised (one row, reviewer "Dana Fox") under a LIVE run
(`E2E_LIVE_AI=1` + `ANTHROPIC_API_KEY`), which a live Locator can satisfy by
emitting a `rating_ref` onto the `aria-label="5 out of 5 stars"` cue span.

### Remaining MANUAL / live verification step

To prove the rating filter matches exactly the single 5-star row under live AI:

```
# with the stack started in live-AI mode and a valid ANTHROPIC_API_KEY
cd e2e && E2E_LIVE_AI=1 npx playwright test ingestion-summary.spec.ts
```

Making the filter genuinely match a row UNDER THE STUB would require a change
outside the one in-scope file (teach `backend/app/core/ai_stub.py`, owned by
`platform-foundation`, to attach a cue-backed `rating_ref`), which is out of
scope for this step and would need to be raised with that spec. See plan.md
"PROMINENT FINDING" for the full analysis and options.
