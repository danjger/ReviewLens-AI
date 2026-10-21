# Extraction evaluation report

- Run mode: **live (AI-backed methods scored)**
- Pages scored: **21**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

- Precision on `will_work` pages: **99.3%** (threshold ≥ 98%)
- Recall on `will_work` pages: **100.0%** (threshold ≥ 90%)
- Thresholds met: **yes**

## Viability verdict accuracy (dataset-ingestion Requirement 3.14)

- Verdict accuracy: **100.0%** (21/21 pages, threshold ≥ 90%)
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
| real_g2_style | `will_work` | `will_work` | ✓ |
| real_marketplace_photos | `limited` | `limited` | ✓ |
| real_maps_local | `limited` | `limited` | ✓ |
| real_infinite_scroll | `will_work` | `will_work` | ✓ |
| real_multilang_dates | `limited` | `limited` | ✓ |
| real_app_store | `limited` | `limited` | ✓ |
| real_healthcare_provider | `limited` | `limited` | ✓ |
| real_course_platform | `limited` | `limited` | ✓ |
| real_webscrapingdev_product | `will_work` | `will_work` | ✓ |
| real_webscrapingdev_reviews | `will_work` | `will_work` | ✓ |
| real_webscrapingdev_testimonials | `will_work` | `will_work` | ✓ |

## Totals by method

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 10.1% | 100.0% (17/17) | 100.0% (17/17) | 100.0% (12/12) | ✗ | 17 | 0 | 13 ms |
| selectors | 100.0% | 100.0% | 60.8% (101/166) | 93.0% (107/115) | 100.0% (99/99) | ✓ | 169 | 0 | 724 ms |
| ai_direct | 100.0% | 100.0% | 12.7% (21/166) | 60.0% (69/115) | 15.2% (15/99) | ✓ | 169 | 109219 | 138515 ms |
| auto | 98.8% | 100.0% | 15.7% (26/166) | 60.0% (69/115) | 16.2% (16/99) | ✓ | 171 | 145219 | 180527 ms |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 17 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4653 | 7051 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4627 | 6180 ms |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4728 | 6710 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4730 | 6454 ms |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4472 | 6534 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 8959 | 12359 ms |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4399 | 6479 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4343 | 5941 ms |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3738 | 4793 ms |
| auto | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3709 | 4515 ms |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 8 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4632 | 6558 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4628 | 6421 ms |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4911 | 7092 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 9779 | 13932 ms |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3094 | 1545 ms |
| auto | — | — | — | — | — | ✓ | 0 | 6243 | 3243 ms |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3106 | 2607 ms |
| auto | — | — | — | — | — | ✓ | 0 | 9365 | 6680 ms |

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 24 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7627 | 16104 ms |
| auto | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7134 | 13335 ms |

### real_g2_style

- Verdict: `will_work` · Tags: structured_jsonld, plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5550 | 6568 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5595 | 6665 ms |

### real_marketplace_photos

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 0 | 594 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 5065 | 6574 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5197 | 8582 ms |

### real_maps_local

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 4648 | 6203 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 9434 | 12785 ms |

### real_infinite_scroll

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 12 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 4761 | 5631 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 4843 | 6067 ms |

### real_multilang_dates

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 25.0% (1/4) | 100.0% (5/5) | ✓ | 5 | 0 | 18 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5016 | 6189 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 4936 | 5499 ms |

### real_app_store

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 4643 | 6468 ms |
| auto | 83.3% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 6 | 4603 | 6096 ms |

### real_healthcare_provider

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5065 | 6264 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 10134 | 12176 ms |

### real_course_platform

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 5 | 4477 | 6389 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 5 | 4481 | 6230 ms |

### real_webscrapingdev_product

- Verdict: `will_work` · Tags: plain_list, js_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | — | ✓ | 5 | 9229 | 5677 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | — | ✓ | 5 | 9225 | 5448 ms |

### real_webscrapingdev_reviews

- Verdict: `will_work` · Tags: plain_list, js_rendered, multipage

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/20) | 100.0% (20/20) | — | ✓ | 20 | 0 | 12 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 7146 | 10862 ms |
| auto | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 14714 | 24127 ms |

### real_webscrapingdev_testimonials

- Verdict: `will_work` · Tags: plain_list, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 3 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 0 | 2 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 8259 | 6216 ms |
| auto | 97.6% | 100.0% | 0.0% (0/40) | — | — | ✓ | 41 | 8540 | 7792 ms |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
