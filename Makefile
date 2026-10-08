.PHONY: up down lint test test-int test-scale e2e eval synth record-ai help

# ── Local stack ──────────────────────────────────────────────────────────────

## up: Start the full local stack (Docker Compose)
up:
	docker compose up --build -d

## down: Stop the full local stack
down:
	docker compose down

# ── Lint & type-check ────────────────────────────────────────────────────────

## lint: Run ruff, mypy (backend); eslint, tsc (frontend)
lint: lint-backend lint-frontend

lint-backend:
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .
	cd backend && uv run mypy app/

lint-frontend:
	cd frontend && npm run lint
	cd frontend && npm run type-check

# ── Tests ────────────────────────────────────────────────────────────────────

## test: Backend and frontend unit + property tests
test: test-backend test-frontend

test-backend:
	cd backend && uv run pytest tests/unit tests/property -v

test-frontend:
	cd frontend && npm test

## test-int: Integration tests against the Compose stack
test-int:
	docker compose up -d --wait
	cd backend && uv run pytest tests/integration -v -m integration

## test-scale: Two consumer instances against shared queues
test-scale:
	docker compose up -d --wait
	cd backend && uv run pytest tests/scale -v -m scale

# ── E2E ──────────────────────────────────────────────────────────────────────

## e2e: Playwright end-to-end tests (AI stubbed unless E2E_LIVE_AI=1)
e2e:
	cd e2e && npx playwright test

# ── Eval ─────────────────────────────────────────────────────────────────────

## eval: Extraction and guardrail evaluation suites (live AI, needs ANTHROPIC_API_KEY)
eval:
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
	  echo "Error: ANTHROPIC_API_KEY is not set"; exit 1; \
	fi
	cd backend && uv run pytest evals/ -v -m live_ai

## record-ai: Record AI fixtures for tests (uses live model, needs ANTHROPIC_API_KEY)
record-ai:
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
	  echo "Error: ANTHROPIC_API_KEY is not set"; exit 1; \
	fi
	cd backend && RECORD_AI=1 uv run pytest tests/ -v -m live_ai

# ── Infrastructure ───────────────────────────────────────────────────────────

## synth: CDK synth + CDK assertion tests
synth:
	cd infra && npm ci && npm test && npx cdk synth

# ── Help ─────────────────────────────────────────────────────────────────────

## help: Show this help
help:
	@grep -E '^## ' Makefile | sed 's/^## /  /'
