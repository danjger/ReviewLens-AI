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
| structured | 100.0% | 10.1% | 100.0% (17/17) | 100.0% (17/17) | 100.0% (12/12) | ✗ | 17 | 0 | 24 ms |
| selectors | 100.0% | 100.0% | 60.8% (101/166) | 93.0% (107/115) | 100.0% (99/99) | ✓ | 169 | 0 | 903 ms |
| ai_direct | 100.0% | 100.0% | 12.7% (21/166) | 60.0% (69/115) | 21.2% (21/99) | ✓ | 169 | 113336 | 136451 ms |
| auto | 98.3% | 100.0% | 12.7% (21/166) | 60.0% (69/115) | 6.1% (6/99) | ✓ | 172 | 141202 | 172009 ms |

## Per-page scores

### structured_jsonld

- Verdict: `will_work` · Tags: structured_jsonld

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 18 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4671 | 7272 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4602 | 5726 ms |

### structured_microdata

- Verdict: `will_work` · Tags: structured_microdata

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4728 | 6499 ms |
| auto | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4724 | 6221 ms |

### js_rendered

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4476 | 6188 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4476 | 5993 ms |

### plain_list

- Verdict: `will_work` · Tags: plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 9 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 4403 | 6198 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 0.0% (0/6) | 0.0% (0/6) | ✓ | 6 | 8757 | 11951 ms |

### no_ratings

- Verdict: `limited` · Tags: no_ratings, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | — | — | 100.0% (3/3) | ✓ | 3 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3731 | 4467 ms |
| auto | 100.0% | 100.0% | — | — | 0.0% (0/3) | ✓ | 3 | 3733 | 4357 ms |

### multipage_listing

- Verdict: `will_work` · Tags: multipage, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✗ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 9 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4634 | 6468 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4620 | 6058 ms |

### mixed_qa_seller

- Verdict: `will_work` · Tags: mixed_qa_seller, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 4861 | 6737 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 0.0% (0/6) | ✓ | 6 | 9778 | 13485 ms |

### blocker_consent

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3094 | 1318 ms |
| auto | — | — | — | — | — | ✓ | 0 | 9641 | 5245 ms |

### blocker_empty

- Verdict: `wont_work` · Tags: blocker

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | — | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | _no labelled selectors_ | — | — | — | — | — | — | — | — |
| ai_direct | — | — | — | — | — | ✓ | 0 | 3106 | 2387 ms |
| auto | — | — | — | — | — | ✓ | 0 | 6184 | 4233 ms |

### large_server_rendered

- Verdict: `will_work` · Tags: plain_list, large_server_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | ✓ | 24 | 0 | 24 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7133 | 12903 ms |
| auto | 100.0% | 100.0% | 0.0% (0/24) | 100.0% (24/24) | 0.0% (0/24) | ✓ | 24 | 7627 | 15889 ms |

### real_g2_style

- Verdict: `will_work` · Tags: structured_jsonld, plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 0 | 3 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 5539 | 6160 ms |
| auto | 100.0% | 100.0% | 0.0% (0/6) | 100.0% (6/6) | 100.0% (6/6) | ✓ | 6 | 5560 | 6254 ms |

### real_marketplace_photos

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 1 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 0 | 753 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 100.0% (5/5) | ✓ | 5 | 5137 | 7029 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | 0.0% (0/5) | ✓ | 5 | 5125 | 6740 ms |

### real_maps_local

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 4720 | 6322 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | — | 0.0% (0/5) | ✓ | 5 | 4673 | 6003 ms |

### real_infinite_scroll

- Verdict: `will_work` · Tags: js_rendered, plain_list

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 11 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 4798 | 5560 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 4804 | 5531 ms |

### real_multilang_dates

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 25.0% (1/4) | 100.0% (5/5) | ✓ | 5 | 0 | 28 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 100.0% (5/5) | ✓ | 5 | 5018 | 6095 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/4) | 0.0% (0/5) | ✓ | 5 | 4964 | 5449 ms |

### real_app_store

- Verdict: `limited` · Tags: plain_list, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) | ✓ | 5 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 5 | 9330 | 11752 ms |
| auto | 83.3% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | 0.0% (0/5) | ✓ | 6 | 4602 | 6228 ms |

### real_healthcare_provider

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 4 ms |
| ai_direct | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 5020 | 5815 ms |
| auto | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 10055 | 11243 ms |

### real_course_platform

- Verdict: `limited` · Tags: plain_list, no_ratings, mixed_qa_seller

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 0 ms |
| selectors | 100.0% | 100.0% | 100.0% (5/5) | — | 100.0% (5/5) | ✓ | 5 | 0 | 0 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 5 | 4477 | 6035 ms |
| auto | 83.3% | 100.0% | 0.0% (0/5) | — | 0.0% (0/5) | ✓ | 6 | 4489 | 6130 ms |

### real_webscrapingdev_product

- Verdict: `will_work` · Tags: plain_list, js_rendered

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | 100.0% | 100.0% | 100.0% (5/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/5) | 100.0% (5/5) | — | ✓ | 5 | 0 | 6 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | — | ✓ | 5 | 9225 | 5685 ms |
| auto | 100.0% | 100.0% | 0.0% (0/5) | 0.0% (0/5) | — | ✓ | 5 | 9270 | 5568 ms |

### real_webscrapingdev_reviews

- Verdict: `will_work` · Tags: plain_list, js_rendered, multipage

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 2 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/20) | 100.0% (20/20) | — | ✓ | 20 | 0 | 18 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 7030 | 9870 ms |
| auto | 100.0% | 100.0% | 0.0% (0/20) | 0.0% (0/20) | — | ✓ | 20 | 15153 | 27236 ms |

### real_webscrapingdev_testimonials

- Verdict: `will_work` · Tags: plain_list, no_ratings

| Method | Precision | Recall | Rating | Date | Author | Next page | Extracted | AI tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| structured | — | 0.0% | — | — | — | ✓ | 0 | 0 | 6 ms |
| selectors | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 0 | 2 ms |
| ai_direct | 100.0% | 100.0% | 0.0% (0/40) | — | — | ✓ | 40 | 8205 | 5690 ms |
| auto | 97.6% | 100.0% | 0.0% (0/40) | — | — | ✓ | 41 | 8365 | 6471 ms |

> The default extraction strategy should be the best-scoring method in this report (Requirement 8.5). The automatic choice is `auto`.
