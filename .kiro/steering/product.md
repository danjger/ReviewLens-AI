---
inclusion: always
---

# Product: ReviewLens AI

ReviewLens AI is a web portal for an Online Reputation Management consultancy. An analyst gives it review-page URLs (or a CSV of reviews). The app checks whether it can read the reviews, collects them, summarizes them, and lets anyone ask questions answered **only** from those reviews.

The original brief is in #[[file:docs/brief.md]].

## Who uses it

Consultancy analysts, and reviewers of this project. There are no accounts: anyone with the link uses the app, and everyone sees the same datasets, check results, and Q&A history.

## Core capabilities

1. **URL check** – paste up to 10 URLs; each gets a verdict (Will work / Limited / Won't work) from AI reading of the rendered page, with reasons and sample reviews.
2. **Ingestion** – add viable URLs or upload a CSV. A URL already tracked is never duplicated; adding it again refreshes the original.
3. **Processing** – collect up to 10 pages, extract reviews, profile the product, compute sentiment and themes.
4. **Library** – one list of tracked datasets with live status; open, refresh, archive, restore. New-URL entry is visually separate from the tracked list.
5. **Summary** – what was ingested, how complete it is, predicted vs. actual extraction, and the reviews themselves.
6. **Guardrailed Q&A** – answers grounded only in the dataset's reviews, with citations; anything outside scope is declined politely. Shared history shows when the data was refreshed.

## Decisions already made (don't revisit without asking)

- No sign-in. Abuse and cost are controlled by WAF, per-IP and global rate limits, a budget alarm, and an AI spend limit.
- Review detection is AI-first. No site-specific parsers are required; extraction quality is measured by the evaluation suite.
- Every backend service is stateless and runs unchanged on Lambda or in containers. Lambda is the first deployment.
- Database is Aurora PostgreSQL Serverless v2 through the RDS Data API.
- AI provider is Anthropic Claude.

## What "good" looks like

- The scope guard never answers from outside the reviews.
- Review text shown anywhere is always copied from the source page, never generated.
- An analyst can always tell how fresh and how complete the data is.
