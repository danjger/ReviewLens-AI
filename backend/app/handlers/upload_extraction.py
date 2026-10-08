"""Upload extraction stage of the processing pipeline (review-analysis task 3.2).

A dataset whose ``source_type`` is ``upload`` has no review *pages* to crawl or
read with the Extraction Engine. Its reviews come from the CSV the analyst
uploaded plus the column mapping confirmed at submit time. dataset-ingestion
copied both into the dataset's permanent location and wrote ``mapping.json``
(``{"mapping": {field: header}, "keep_rule": ..., "will_keep": ...}``); this
stage reads them back and builds the Normalized Reviews.

Implements Requirement 3.4: build Normalized Reviews from the saved column
mapping, skip rows whose review text is empty (counting them), and keep at most
``MAX_REVIEWS`` rows using the keep rule shown in the upload preview, recording a
warning with the number of rows left out.

Scope (task 3.2): this stage *only* turns the upload into reviews plus counts.
It does not dedupe (task 3.3), compute metrics, or write ``reviews/v{n}.json``
(task 5). It returns :class:`~app.handlers.extraction_stage.CollectedReview`
objects — the same shape the per-page extraction stage returns — so the dedupe
and output stages consume URL and upload datasets through one path (design
flowchart: both the ``upload`` and ``url`` branches feed the dedupe node ``D``).

Engineering rules honoured:

* **Stateless** — the stage holds no state beyond its inputs; the CSV and
  mapping live in S3 and are read fresh each run, so a redelivery rebuilds the
  same reviews.
* **Review text from the file, by code** — every review's text is copied
  verbatim from a CSV cell; nothing is AI-generated (product rule: "Review text
  shown anywhere is always copied from the source page, never generated").
* **S3 keys from** :mod:`app.storage.keys` — the CSV and mapping keys are built
  there, never by hand.
* **Reuse** — the keep-rule labels and CSV parsing come from
  :mod:`app.ingestion.upload_parser`; this stage does not re-implement either.

Source page convention: uploads have no pages, so each review's ``source_page``
is :data:`UPLOAD_SOURCE_PAGE` (``0``). A real captured page is always ``>= 1``
(page 1 is the Check's first page), so ``0`` unambiguously marks an upload-origin
review in ``reviews/v{n}.json``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.extraction.models import VerifiedReview
from app.handlers.extraction_stage import CollectedReview
from app.ingestion.upload_parser import (
    AUTHOR,
    DATE,
    KEEP_MOST_RECENT,
    RATING,
    TEXT,
    TITLE,
)
from app.storage import keys, s3

logger = logging.getLogger(__name__)

#: ``source_page`` recorded for every upload-origin review. Uploads have no
#: captured pages; a real page is always ``>= 1``, so ``0`` marks "from the
#: uploaded file" without colliding with a page number.
UPLOAD_SOURCE_PAGE = 0


@dataclass(frozen=True)
class UploadExtractionResult:
    """What the upload extraction stage produced.

    Attributes:
        reviews: The Normalized Reviews built from the kept CSV rows, each tagged
            with :data:`UPLOAD_SOURCE_PAGE`. In keep-rule order (see
            :func:`extract_upload`). Not yet deduped (task 3.3).
        skipped_empty: Count of data rows skipped because their mapped review
            text was empty (Requirement 3.4).
        left_out: Count of usable rows dropped by the ``MAX_REVIEWS`` keep rule
            (``0`` when the file is at or under the cap).
        warnings: One warning recording the number of rows left out, present
            only when ``left_out > 0`` (Requirement 3.4).
    """

    reviews: list[CollectedReview] = field(default_factory=list)
    skipped_empty: int = 0
    left_out: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _UploadMapping:
    """The parsed ``mapping.json`` document for an upload dataset version."""

    mapping: dict[str, str]
    keep_rule: str


def _parse_rating(value: str) -> float | None:
    """Parse a rating cell to a float, or ``None`` when it is not a number.

    Ratings in uploads are free text; a non-numeric or empty cell simply means
    the row has no rating (the field is optional, Requirement 7.2 of
    dataset-ingestion), so it is dropped rather than failing the row.
    """
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _optional(value: str) -> str | None:
    """Return a trimmed non-empty cell, or ``None`` when the cell is blank."""
    stripped = value.strip()
    return stripped or None


def _build_review(row: dict[str, str], mapping: dict[str, str]) -> VerifiedReview:
    """Build one Normalized Review from a CSV *row* using the column *mapping*.

    Text is copied verbatim (only outer whitespace trimmed) from the mapped text
    column. Optional fields are read from their mapped columns when the mapping
    names them and the cell is non-empty; a missing or blank optional cell
    leaves the field ``None``. ``source_ref`` is left ``None`` — an upload has no
    page element reference.
    """
    text = row[mapping[TEXT]].strip()
    rating = _parse_rating(row[mapping[RATING]]) if RATING in mapping else None
    date = _optional(row[mapping[DATE]]) if DATE in mapping else None
    author = _optional(row[mapping[AUTHOR]]) if AUTHOR in mapping else None
    title = _optional(row[mapping[TITLE]]) if TITLE in mapping else None
    return VerifiedReview(
        text=text,
        rating=rating,
        date=date,
        author=author,
        title=title,
    )


def _rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    """Parse CSV *raw* bytes into header→value dict rows.

    Decoding and delimiter sniffing reuse :mod:`app.ingestion.upload_parser`'s
    behaviour (UTF-8 BOM-tolerant, comma/tab sniffed) through ``csv.DictReader``
    so the same file the preview counted is read the same way here. Short rows
    (missing trailing cells) yield empty strings for the absent columns, so the
    review builder and the empty-text check never see ``None``.
    """
    from app.ingestion.upload_parser import _sniff_delimiter

    text = raw.decode("utf-8-sig")
    delimiter = _sniff_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        rows.append({key: (val or "") for key, val in raw_row.items() if key is not None})
    return rows


def _load_mapping(dataset_id: str, version: int) -> _UploadMapping:
    """Read and validate ``mapping.json`` for an upload dataset version.

    The key comes from :func:`app.storage.keys.dataset_raw_mapping`. A document
    without a usable text mapping cannot build reviews, so it is a hard error
    (the file was validated at submit time, so this only fires on corruption).
    """
    doc = json.loads(s3.get_text(keys.dataset_raw_mapping(dataset_id, version)))
    mapping = doc.get("mapping")
    if not isinstance(mapping, dict) or TEXT not in mapping:
        raise ValueError(f"mapping.json for {dataset_id!r} v{version} has no review-text column")
    keep_rule = doc.get("keep_rule")
    return _UploadMapping(mapping=mapping, keep_rule=str(keep_rule))


def _apply_keep_rule(
    reviews: list[VerifiedReview],
    keep_rule: str,
    max_reviews: int,
) -> tuple[list[VerifiedReview], int]:
    """Keep at most *max_reviews* reviews using *keep_rule* (Requirement 3.4).

    Mirrors the keep rule shown in the upload preview
    (:mod:`app.ingestion.upload_parser`):

    * ``most_recent_by_date`` — when a date column was mapped, keep the most
      recent rows by date. Dates are ISO-like strings; they sort lexicographically
      in chronological order for the common ``YYYY-MM-DD`` form. Rows with no
      parseable date sort oldest (dropped first). The order among kept rows is
      left as file order so the output is stable.
    * ``first_in_file`` — otherwise keep the first rows in file order.

    Returns the kept reviews (in file order for a stable result) and the number
    of rows left out (``0`` when at or under the cap).
    """
    if len(reviews) <= max_reviews:
        return reviews, 0

    left_out = len(reviews) - max_reviews

    if keep_rule == KEEP_MOST_RECENT:
        # Rank by date descending (most recent first); blanks rank oldest. Use
        # a stable sort on the original index so ties keep file order, then take
        # the top ``max_reviews`` and restore file order for stable output.
        indexed = list(enumerate(reviews))
        indexed.sort(key=lambda pair: ((pair[1].date or ""), -pair[0]), reverse=True)
        kept_indices = sorted(index for index, _ in indexed[:max_reviews])
        kept = [reviews[index] for index in kept_indices]
        return kept, left_out

    # first_in_file (and any unknown rule falls back to this safe default).
    return reviews[:max_reviews], left_out


def extract_upload(dataset_id: str, version: int) -> UploadExtractionResult:
    """Build Normalized Reviews for an upload dataset version (Requirement 3.4).

    Reads ``mapping.json`` and the uploaded CSV from the dataset's own S3 objects
    (keys from :mod:`app.storage.keys`), builds a Normalized Review for every row
    whose mapped review text is non-empty (counting the rows it skips), then
    keeps at most ``MAX_REVIEWS`` rows using the keep rule saved with the file,
    recording a warning with the number of rows left out.

    Review text is copied verbatim from the CSV cell; no field is AI-generated.

    Args:
        dataset_id: The upload dataset's id.
        version: The data version being processed (its CSV and mapping live under
            ``datasets/{id}/raw/v{version}/``).

    Returns:
        An :class:`UploadExtractionResult` with the kept reviews (tagged with
        :data:`UPLOAD_SOURCE_PAGE`, not yet deduped), the skipped-empty count,
        the left-out count, and a warning when rows were left out.

    Raises:
        ValueError: ``mapping.json`` names no review-text column (corruption; the
            file was validated at submit time).
    """
    max_reviews = get_settings().max_reviews
    upload_mapping = _load_mapping(dataset_id, version)
    raw = s3.get_bytes(keys.dataset_raw_upload(dataset_id, version))

    text_header = upload_mapping.mapping[TEXT]
    rows = _rows_from_csv(raw)

    # Build a review for every row with non-empty text; count the empties.
    skipped_empty = 0
    built: list[VerifiedReview] = []
    for row in rows:
        if not row.get(text_header, "").strip():
            skipped_empty += 1
            continue
        built.append(_build_review(row, upload_mapping.mapping))

    kept, left_out = _apply_keep_rule(built, upload_mapping.keep_rule, max_reviews)

    warnings: list[str] = []
    if left_out > 0:
        warnings.append(
            f"Upload has more than {max_reviews} reviews; kept {len(kept)} "
            f"and left out {left_out} using the {upload_mapping.keep_rule} rule."
        )

    reviews = [CollectedReview(review=review, source_page=UPLOAD_SOURCE_PAGE) for review in kept]

    logger.debug(
        "upload extraction %s v%d: %d kept, %d skipped empty, %d left out (keep_rule=%s)",
        dataset_id,
        version,
        len(reviews),
        skipped_empty,
        left_out,
        upload_mapping.keep_rule,
    )

    return UploadExtractionResult(
        reviews=reviews,
        skipped_empty=skipped_empty,
        left_out=left_out,
        warnings=warnings,
    )
