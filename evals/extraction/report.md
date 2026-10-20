# Extraction evaluation report

- Run mode: **live (AI-backed methods scored)**
- Pages scored: **21**
- Layout coverage: **all required layout types present**

## Automatic choice vs. thresholds (Requirement 8.3)

- Precision on `will_work` pages: **99.2%** (threshold ≥ 98%)
- Recall on `will_work` pages: **86.0%** (threshold ≥ 90%)
- Thresholds met: **no**

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
| real_infinite_scroll | `will_work` | `will_work` | ✓ |
| real_multilang_dates | `limited` | `limited` | ✓ |
| real_app_store | `limited` | `limited` | ✓ |
| real_healthcare_provider | `limited` | `limited` | ✓ |
| real_course_platform | `limited` | `limited` | ✓ |
| real_webscrapingdev_product | `will_work` | `will_work` | ✓ |
| real_webscrapingdev_reviews | `will_work` | `limited` | ✗ |
| real_webscrapingdev_testimonials | `will_work` | `will_work` | ✓ |

## Totals by method

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 10.1% | 100.0% (17/17) | 100.0% (17/17) | 100.0% (12/12) | ✗ | 17 | 0 | 16 ms |
| selectors | 100.0% | 100.0% | 60.8% (101/166) | 93.0% (107/115) | 100.0% (99/99) | ✓ | 169 | 0 | 695 ms |
| ai_direct | 99.4% | 100.0% | 9.0% (15/166) | 60.0% (69/115) | 16.2% (16/99) | ✓ | 170 | 130868 | 71264 ms |
| auto | 98.7% | 88.8% | 17.7% (26/147) | 78.1% (75/96) | 10.1% (10/99) | ✓ | 152 | 150261 | 72840 ms |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 16 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5450 | 3716 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5453 | 2827 ms |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5579 | 3040 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5568 | 2727 ms |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 5204 | 3383 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 5206 | 2629 ms |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 9 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 5073 | 2725 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 10186 | 5144 ms |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 5 ms |
| ai_direct | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 4408 | 2280 ms |
| auto | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 4409 | 2004 ms |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 9 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5264 | 2704 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5297 | 2584 ms |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 7 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5686 | 2925 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 5724 | 2916 ms |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3732 | 1680 ms |
| auto | — | — | — | — | — | ✓ | 0 | 7464 | 2293 ms |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3691 | 1697 ms |
| auto | — | — | — | — | — | ✓ | 0 | 7097 | 3528 ms |

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 31 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 8467 | 6325 ms |
| auto | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 8398 | 5855 ms |

### real_g2_style

- Verdict: `will_work` · Tags: structured_jsonld, plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 6554 | 2956 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 6562 | 3011 ms |

### real_marketplace_photos

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 0 | 543 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5870 | 3197 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5871 | 2880 ms |

### real_maps_local

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 5558 | 2964 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 5556 | 2749 ms |

### real_infinite_scroll

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 11 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5678 | 2835 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5681 | 2532 ms |

### real_multilang_dates

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 25.0% (1/4) | 100.0% (5/5) | ✓ | 5 | 0 | 18 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5842 | 2876 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5842 | 2669 ms |

### real_app_store

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5412 | 2753 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 5422 | 2661 ms |

### real_healthcare_provider

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5937 | 2902 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5917 | 2930 ms |

### real_course_platform

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 5 | 5254 | 2791 ms |
| auto | 83.3% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 6 | 5273 | 2685 ms |

### real_webscrapingdev_product

- Verdict: `will_work` · Tags: plain_list, js_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 3 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 7 ms |
| ai_direct | 83.3% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | — | ✓ | 6 | 11357 | 3489 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 11066 | 2661 ms |

### real_webscrapingdev_reviews

- Verdict: `will_work` · Tags: plain_list, js_rendered, multipage

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/20) | 100.0% (20/20) | — | ✓ | 20 | 0 | 12 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 8701 | 5268 ms |
| auto | 100.0% | 5.0% | 0.0% (0/1) | 100.0% (1/1) | — | ✓ | 1 | 16113 | 6987 ms |

### real_webscrapingdev_testimonials

- Verdict: `will_work` · Tags: plain_list, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 5 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 12151 | 8760 ms |
| auto | 97.6% | 100.0% | 0.0% (0/40) | — | — | ✓ | 41 | 12156 | 8568 ms |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
