# Adding real review pages to the extraction eval set (Task 9.3)

This is the hands-on guide for `review-extraction` task 9.3: take review-page
URLs you have, save each page's **rendered** HTML into the eval set, label it by
hand, and publish it so tests can load it over HTTP. The goal is **at least 15
labeled pages total**. You already have 9 synthetic pages, so you need **≥6
real ones**.

Everything the tooling needs already exists (`/evals/extraction/`); this task is
purely "add pages + labels + publish", which needs a person to pick real sites
and hand-check the labels.

---

## The big gotcha: capture RENDERED HTML, not raw HTML

The extraction engine reads the page **after JavaScript has run** (the
browser's final DOM). Many review sites render reviews with JavaScript, so a
plain `curl https://...` or "View Source" gives you a near-empty shell with no
reviews — that would make a useless eval page.

**Rule of thumb:** always capture the HTML the way the browser shows it, not the
raw download. Two reliable ways below; prefer the Playwright script because it's
repeatable and matches how the app itself will render pages.

### Option A — Playwright (recommended, repeatable)

Playwright + Chromium are already project dependencies (the workers image uses
them). From the repo root:

```bash
cd backend
uv run python - <<'PY'
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

# EDIT THESE TWO:
URL = "https://example.com/product/reviews"
NAME = "acme_widget"   # short slug; becomes pages/<NAME>/

out = Path("..") / "evals" / "extraction" / "pages" / NAME
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(URL, wait_until="networkidle", timeout=60000)
    # If reviews load on scroll or behind a "see more", nudge the page:
    page.mouse.wheel(0, 20000)
    page.wait_for_timeout(1500)
    html = page.content()          # the RENDERED DOM, post-JavaScript
    browser.close()

(out / "page.html").write_text(html, encoding="utf-8")
print(f"saved {out/'page.html'} ({len(html)} bytes) for {URL}")
PY
```

Install the browser once if you haven't: `uv run playwright install chromium`.

If a site shows a cookie/consent wall or login wall and you can't get past it,
that's fine — **capture the wall**. A blocker page is a required layout type
(label it `wont_work`, see below).

### Option B — your own browser (quick, manual)

1. Open the URL in Chrome/Firefox.
2. Scroll so all the reviews you want are loaded.
3. Open DevTools (F12) → **Elements** tab → right-click the top `<html>` node →
   **Copy → Copy outerHTML** (this copies the *rendered* DOM, not the raw
   source — important).
4. Create `evals/extraction/pages/<name>/page.html` and paste.

Do NOT use browser "Save Page As" (it rewrites asset URLs and can inline things
oddly) or "View Source" (raw, pre-JavaScript).

### Trim it down (optional but kind)

You can delete obviously irrelevant chunks (giant inline `<script>`/`<style>`
blobs, unrelated marketing sections) to keep the file small, **but never edit
the review content** — the labels must match what's really on the page. When in
doubt, leave it; the cleaner strips scripts/styles anyway.

> Privacy: these pages get committed and published. Avoid pages that contain
> personal data about private individuals beyond the public review text/author
> name already shown. Don't capture anything behind your own logged-in session.

---

## Where files go

```
evals/extraction/
  pages/
    <name>/
      page.html          # the rendered HTML you captured
  labels.yaml            # one entry per <name>  (you add to this)
```

`<name>` is a short slug (lowercase, underscores). It must match between the
directory and the `labels.yaml` key.

---

## Labeling: add an entry to labels.yaml

Open `evals/extraction/labels.yaml` and add one top-level entry per page. Here
is the full schema with every field explained:

```yaml
acme_widget:                      # must equal pages/<name>/
  url: https://example.com/product/reviews   # the real page URL
  verdict: will_work              # will_work | limited | wont_work  (your judgement)
  type: plain_list                # free-form primary layout label
  tags: [plain_list]              # layout tags (see the required set below)
  next_page: https://example.com/product/reviews?page=2   # or null
  # OPTIONAL: known-good CSS selectors, used to score the offline "selectors"
  # method. Omit the whole block if you don't want to hand-write selectors —
  # the structured + (live) ai_direct methods still score without them.
  selectors:
    item: ".review"               # wrapper element for ONE review
    text: ".text"                 # within an item: the review body
    rating: ".rating"             # within an item: the rating element
    date: ".date"
    author: ".author"
  reviews:                        # the reviews you can SEE on the page, in order
    - prefix: "Grinds evenly and quietly, and the burrs feel"   # leading text, enough to be unique
      rating: 5                   # omit if the page shows no rating
      date: "2024-03-03"          # ISO yyyy-mm-dd if you can; else omit
      author: "Helen R."          # omit if absent
    - prefix: "Great grind consistency for pour-over, but the hopper"
      rating: 4
      date: "2024-02-19"
      author: "Tomás G."
```

### Field rules

- **url** — the real URL (absolute). Used as the extraction URL and for the
  same-domain next-page check.
- **verdict** — your hand-checked call:
  - `will_work` — clear review list the engine should extract well.
  - `limited` — reviews exist but something's awkward (e.g. no ratings, sparse).
  - `wont_work` — a blocker (captcha / login / consent wall) or no reviews.
- **tags** — layout tags. Across ALL your real pages, together with the 9
  synthetic ones, the set must cover these 8 required tags (the suite already
  covers them, but keep variety as you add real pages):
  `structured_jsonld`, `structured_microdata`, `js_rendered`, `plain_list`,
  `no_ratings`, `multipage`, `mixed_qa_seller`, `blocker`.
  A page can carry several tags, e.g. `[js_rendered, plain_list]`.
- **next_page** — the absolute URL of the next page of reviews, or `null` if the
  page has no URL-based next page (infinite scroll / "Load more" with no link,
  or a single-page listing).
- **selectors** — optional. Only `item` is required if you include the block.
  `text`/`rating`/`date`/`author` are matched *within* each item. If you skip
  this, the offline "selectors" method just reports "no labelled selectors" for
  that page, which is fine.
- **reviews** — the list of reviews actually visible on the page, in page order:
  - **prefix** (required) — a leading substring of the review's visible text,
    long enough to identify the review unambiguously. It does not need to be the
    whole review. Matching is by normalized containment on this prefix.
  - **rating** — the numeric rating as shown (e.g. `5`, `4.5`). Omit if the page
    shows no rating for that review.
  - **date** — prefer ISO `yyyy-mm-dd`; the scorer matches leniently, so a close
    form is OK, but ISO is cleanest. Omit if absent.
  - **author** — the author name as shown. Omit if absent.
  - For a **blocker** page, use `reviews: []` and `verdict: wont_work`.

### How many reviews to label

You don't have to label every review on a long page — label a representative set
you can verify by eye (the first screen's worth is plenty). The scorer computes
recall against what you labeled, so only list reviews you're confident are
really there with the fields you give.

---

## Publish the page to /fixtures-site

After adding `pages/<name>/page.html`, publish all pages so integration/E2E
tests can fetch them over HTTP. From the repo root:

```bash
for d in evals/extraction/pages/*/; do
  name=$(basename "$d")
  mkdir -p "fixtures-site/extraction/$name"
  cp "$d/page.html" "fixtures-site/extraction/$name/index.html"
done
```

(That's the same regeneration command noted in
`fixtures-site/extraction/README.md`.) Then add a link for each new page to the
"Extraction evaluation pages" list in `fixtures-site/index.html`.

---

## Check your work (offline, no API key)

The offline tests validate that your labels parse, every page's HTML loads, and
the layout coverage holds:

```bash
cd backend && uv run pytest tests/unit/evals -q
```

Fix any error it reports (bad verdict value, missing `prefix`, a page dir with
no `page.html`, etc.). This does **not** need an API key — it only checks the
structure and the offline (structured/selectors) scoring.

To eyeball the offline scores and see which pages the selectors/structured
methods already handle:

```bash
python evals/extraction/run.py            # writes evals/extraction/report.md
```

---

## When you're done

- You have ≥15 labeled pages total (`ls evals/extraction/pages | wc -l` ≥ 15).
- `cd backend && uv run pytest tests/unit/evals -q` passes.
- All pages are copied under `fixtures-site/extraction/<name>/index.html` and
  linked from `fixtures-site/index.html`.

That completes task 9.3. The live tuning run (task 9.4) comes next and needs
`ANTHROPIC_API_KEY` — see `docs/aws-deployment.md` / the README for how to
provide the key, then:

```bash
cd backend && ANTHROPIC_API_KEY=sk-ant-... uv run pytest ../evals -v -m live_ai
```
