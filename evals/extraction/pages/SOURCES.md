# Real-page sources and licensing

Most pages in this eval set are synthetic or faithful reconstructions (see each
`page.html` header comment). The pages below are **real, saved copies of a
public web-scraping practice site**, captured with the project's headless
renderer (hydrated HTML, as the capture step would save it) and labeled by hand
from the rendered DOM.

## web-scraping.dev (real)

- `real_webscrapingdev_product`  — https://web-scraping.dev/product/12
- `real_webscrapingdev_reviews`  — https://web-scraping.dev/reviews
- `real_webscrapingdev_testimonials` — https://web-scraping.dev/testimonials

**Source:** web-scraping.dev, a public sandbox published by Scrapfly expressly
for developing and testing web-scraping / extraction tools. The site exists to
be fetched and parsed; its pages contain synthetic product/review content
authored by the site for that purpose (no real persons, no third-party
copyrighted reviews). Captured on 2026-10-10.

**Why these are safe to redistribute here:** the site is designed and published
as practice material for scrapers, and its review/testimonial text is the site's
own synthetic content, not scraped third-party reviews. This avoids the ToS /
copyright exposure that saving and republishing real commercial review-platform
pages (Amazon, G2, Google Maps, etc.) would carry.

**Review text is copied verbatim from the rendered page** (product.md rule:
review text shown anywhere is copied from the source, never generated). The
labels in `labels.yaml` were hand-checked against the rendered DOM:
- product: rating = count of star SVGs per `.review`; date in `<span>`; body in `<p>`.
- reviews: `data-testid` hooks — `review-stars` (SVG count = rating),
  `review-date`, `review-text`.
- testimonials: `span.rating` holds only the filled star SVGs (1–5); text-only
  cards with no author/date (identicon avatar only).

Note: ratings are encoded as SVG star **counts**, which the offline `selectors`
method does not parse (it reads numeric cues from attributes/text), so offline
rating-field accuracy for these pages is 0 by design; the live AI path counts
the stars. Review text precision/recall is the graded signal and is exact.
