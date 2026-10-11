# Extraction evaluation report

- Run mode: **offline (no live AI)**
- Pages scored: **21**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

_Not evaluated: this was an offline run, so the automatic choice was not scored. Run with the live model (`--live`) to evaluate._

## Viability verdict accuracy (dataset-ingestion Requirement 3.14)

_Full-corpus gate not evaluated: this was an offline run, so only blocker pages (no AI call) were scored. Run with the live model (`--live`) to evaluate verdict accuracy across every page._

- Offline-scorable subset accuracy: **100.0%** (2/2 scored pages)

| Page | Expected | Predicted | Correct |
|---|---|---|---|
| structured_jsonld | `will_work` | _skipped (no live AI)_ | — |
| structured_microdata | `will_work` | _skipped (no live AI)_ | — |
| js_rendered | `will_work` | _skipped (no live AI)_ | — |
| plain_list | `will_work` | _skipped (no live AI)_ | — |
| no_ratings | `limited` | _skipped (no live AI)_ | — |
| multipage_listing | `will_work` | _skipped (no live AI)_ | — |
| mixed_qa_seller | `will_work` | _skipped (no live AI)_ | — |
| blocker_consent | `wont_work` | `wont_work` | ✓ |
| blocker_empty | `wont_work` | `wont_work` | ✓ |
| large_server_rendered | `will_work` | _skipped (no live AI)_ | — |
| real_g2_style | `will_work` | _skipped (no live AI)_ | — |
| real_marketplace_photos | `will_work` | _skipped (no live AI)_ | — |
| real_maps_local | `will_work` | _skipped (no live AI)_ | — |
| real_infinite_scroll | `will_work` | _skipped (no live AI)_ | — |
| real_multilang_dates | `will_work` | _skipped (no live AI)_ | — |
| real_app_store | `will_work` | _skipped (no live AI)_ | — |
| real_healthcare_provider | `will_work` | _skipped (no live AI)_ | — |
| real_course_platform | `will_work` | _skipped (no live AI)_ | — |
| real_webscrapingdev_product | `will_work` | _skipped (no live AI)_ | — |
| real_webscrapingdev_reviews | `will_work` | _skipped (no live AI)_ | — |
| real_webscrapingdev_testimonials | `will_work` | _skipped (no live AI)_ | — |

## Totals by method

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 10.1% | 100.0% (17/17) | 100.0% (17/17) | 100.0% (12/12) | ✗ | 17 | 0 | 9 ms |
| selectors | 100.0% | 100.0% | 60.8% (101/166) | 93.0% (107/115) | 100.0% (99/99) | ✓ | 169 | 0 | 631 ms |
| ai_direct | _n/a_ | — | — | — | — | — | — | — | — |
| auto | _n/a_ | — | — | — | — | — | — | — | — |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 16 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 1 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
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

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 14 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_g2_style

- Verdict: `will_work` · Tags: structured_jsonld, plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_marketplace_photos

- Verdict: `will_work` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 0 | 535 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_maps_local

- Verdict: `will_work` · Tags: plain_list, mixed_qa_seller, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_infinite_scroll

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 4 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_multilang_dates

- Verdict: `will_work` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 25.0% (1/4) | 100.0% (5/5) | ✓ | 5 | 0 | 19 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_app_store

- Verdict: `will_work` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 4 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_healthcare_provider

- Verdict: `will_work` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_course_platform

- Verdict: `will_work` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_webscrapingdev_product

- Verdict: `will_work` · Tags: plain_list, js_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 3 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_webscrapingdev_reviews

- Verdict: `will_work` · Tags: plain_list, js_rendered, multipage

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/20) | 100.0% (20/20) | — | ✓ | 20 | 0 | 10 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

### real_webscrapingdev_testimonials

- Verdict: `will_work` · Tags: plain_list, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 3 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 0 | 2 ms |
| ai_direct | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |
| auto | _skipped (no live AI)_ | — | — | — | — | — | — | — | — |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
