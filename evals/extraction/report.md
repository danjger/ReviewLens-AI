# Extraction evaluation report

- Run mode: **offline (no live AI)**
- Pages scored: **9**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

_Not evaluated: this was an offline run, so the automatic choice was not scored. Run with the live model (`--live`) to evaluate._

## Totals by method

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 23.8% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✗ | 5 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 100.0% (18/18) | 100.0% (18/18) | 100.0% (21/21) | ✓ | 21 | 0 | 25 ms |
| ai_direct | _n/a_ | — | — | — | — | — | — | — | — |
| auto | _n/a_ | — | — | — | — | — | — | — | — |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (3/3) | 100.0% (3/3) | 100.0% (3/3) | ✓ | 3 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (3/3) | 100.0% (3/3) | 100.0% (3/3) | ✓ | 3 | 0 | 14 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (2/2) | 100.0% (2/2) | 100.0% (2/2) | ✓ | 2 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (2/2) | 100.0% (2/2) | 100.0% (2/2) | ✓ | 2 | 0 | 1 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (3/3) | 100.0% (3/3) | 100.0% (3/3) | ✓ | 3 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | ✓ | 4 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (3/3) | 100.0% (3/3) | 100.0% (3/3) | ✓ | 3 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (3/3) | 100.0% (3/3) | 100.0% (3/3) | ✓ | 3 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
