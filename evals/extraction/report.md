# Extraction evaluation report

- Run mode: **live (AI-backed methods scored)**
- Pages scored: **10**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

- Precision on `will_work` pages: **100.0%** (threshold ≥ 98%)
- Recall on `will_work` pages: **91.7%** (threshold ≥ 90%)
- Thresholds met: **yes**

## Viability verdict accuracy (dataset-ingestion Requirement 3.14)

- Verdict accuracy: **100.0%** (10/10 pages, threshold ≥ 90%)
- Threshold met: **yes**

| Page | Expected | Predicted | Correct |
|---|---|---|---|
| structured_jsonld | `will_work` | `will_work` | ✓ |
| structured_microdata | `will_work` | `will_work` | ✓ |
| js_rendered | `will_work` | `will_work` | ✓ |
| plain_list | `will_work` | `will_work` | ✓ |
| no_ratings | `limited` | `limited` | ✓ |
| multipage_listing | `will_work` | `will_work` | ✓ |
| mixed_qa_seller | `will_work` | `will_work` | ✓ |
| blocker_consent | `wont_work` | `wont_work` | ✓ |
| blocker_empty | `wont_work` | `wont_work` | ✓ |
| large_server_rendered | `will_work` | `will_work` | ✓ |

## Totals by method

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 19.0% | 100.0% (12/12) | 100.0% (12/12) | 100.0% (12/12) | ✗ | 12 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 100.0% (60/60) | 100.0% (60/60) | 100.0% (63/63) | ✓ | 63 | 0 | 50 ms |
| ai_direct | 100.0% | 100.0% | 10.0% (6/60) | 80.0% (48/60) | 0.0% (0/63) | ✓ | 63 | 45025 | 157 ms |
| auto | 100.0% | 92.1% | 10.9% (6/55) | 87.3% (48/55) | 0.0% (0/58) | ✓ | 58 | 51195 | 186 ms |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 17 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4675 | 28 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4675 | 24 ms |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4616 | 18 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4616 | 20 ms |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4478 | 14 ms |
| auto | 100.0% | 16.7% | 0.0% (0/1) | 0.0% (0/1) | 0.0% (0/1) | ✓ | 1 | 4478 | 10 ms |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4399 | 9 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4399 | 9 ms |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 2 ms |
| ai_direct | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3735 | 8 ms |
| auto | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3735 | 9 ms |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4616 | 15 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4616 | 17 ms |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4925 | 14 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4925 | 17 ms |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3111 | 7 ms |
| auto | — | — | — | — | — | ✓ | 0 | 6222 | 14 ms |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3059 | 6 ms |
| auto | — | — | — | — | — | ✓ | 0 | 6118 | 13 ms |

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 14 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7411 | 37 ms |
| auto | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7411 | 51 ms |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
