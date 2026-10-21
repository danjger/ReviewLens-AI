# Extraction evaluation report

- Run mode: **live (AI-backed methods scored)**
- Pages scored: **21**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

- Precision on `will_work` pages: **100.0%** (threshold ≥ 98%)
- Recall on `will_work` pages: **96.3%** (threshold ≥ 90%)
- Thresholds met: **yes**

## Viability verdict accuracy (dataset-ingestion Requirement 3.14)

- Verdict accuracy: **95.2%** (20/21 pages, threshold ≥ 90%)
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
| real_infinite_scroll | `will_work` | `limited` | ✗ |
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
| structured | 100.0% | 10.1% | 100.0% (17/17) | 100.0% (17/17) | 100.0% (12/12) | ✗ | 17 | 0 | 26 ms |
| selectors | 100.0% | 100.0% | 60.8% (101/166) | 93.0% (107/115) | 100.0% (99/99) | ✓ | 169 | 0 | 203 ms |
| ai_direct | 100.0% | 88.8% | 10.2% (15/147) | 79.2% (76/96) | 22.2% (22/99) | ✓ | 150 | 129289 | 68677 ms |
| auto | 99.4% | 97.0% | 16.8% (27/161) | 63.6% (70/110) | 11.7% (11/94) | ✓ | 165 | 157789 | 79386 ms |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 16 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5467 | 3903 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5453 | 2688 ms |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5578 | 2779 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5574 | 2891 ms |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 5203 | 3496 ms |
| auto | 100.0% | 16.7% | 0.0% (0/1) | 0.0% (0/1) | 0.0% (0/1) | ✓ | 1 | 5220 | 2602 ms |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 10 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 5095 | 2965 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 5095 | 2609 ms |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 4411 | 2497 ms |
| auto | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 4408 | 2151 ms |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 11 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5264 | 2791 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5292 | 2721 ms |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 4 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5685 | 3010 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5724 | 2960 ms |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3732 | 1598 ms |
| auto | — | — | — | — | — | ✓ | 0 | 7464 | 2244 ms |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3534 | 1435 ms |
| auto | — | — | — | — | — | ✓ | 0 | 7097 | 3199 ms |

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 33 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 8253 | 5704 ms |
| auto | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 8368 | 5861 ms |

### real_g2_style

- Verdict: `will_work` · Tags: structured_jsonld, plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 6516 | 2980 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 6566 | 2983 ms |

### real_marketplace_photos

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 0 | 42 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5871 | 3169 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5853 | 2764 ms |

### real_maps_local

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 5544 | 2924 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | — | 0.0% (0/5) | ✓ | 5 | 5542 | 2521 ms |

### real_infinite_scroll

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 10 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5679 | 2749 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5647 | 2493 ms |

### real_multilang_dates

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 25.0% (1/4) | 100.0% (5/5) | ✓ | 5 | 0 | 28 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5824 | 2722 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5823 | 2570 ms |

### real_app_store

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5412 | 2893 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5412 | 2566 ms |

### real_healthcare_provider

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5927 | 2949 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5965 | 2827 ms |

### real_course_platform

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 5 | 5254 | 2743 ms |
| auto | 83.3% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 6 | 5237 | 2672 ms |

### real_webscrapingdev_product

- Verdict: `will_work` · Tags: plain_list, js_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 20.0% (1/5) | — | ✓ | 5 | 11362 | 3713 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 20.0% (1/5) | — | ✓ | 5 | 11378 | 3060 ms |

### real_webscrapingdev_reviews

- Verdict: `will_work` · Tags: plain_list, js_rendered, multipage

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/20) | 100.0% (20/20) | — | ✓ | 20 | 0 | 9 ms |
| ai_direct | 100.0% | 5.0% | 0.0% (0/1) | 0.0% (0/1) | — | ✓ | 1 | 7536 | 2496 ms |
| auto | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 16312 | 7501 ms |

### real_webscrapingdev_testimonials

- Verdict: `will_work` · Tags: plain_list, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 6 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 0 | 2 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 12142 | 9161 ms |
| auto | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 24359 | 17503 ms |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
