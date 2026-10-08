"""Upload parsing and preview building (dataset-ingestion task 8.2).

An analyst uploads a CSV/TSV of reviews straight to S3 through the pre-signed
PUT issued by ``POST /uploads`` (task 8.1). This module reads that staged
object back from S3 and turns it into the **preview** the UI shows before the
analyst submits (``POST /uploads/{upload_id}/preview``, task 8.3), implementing
Requirements 7.2, 7.3, and 7.6:

- **7.2** — a review *text* column is required; *rating*, *date*, *author*, and
  *title* are optional. Columns are detected from common header names (the
  :data:`_SYNONYMS` table) and the analyst may correct the mapping afterwards.
- **7.3** — a file with no usable text column, that cannot be parsed, or that
  exceeds the ``MAX_UPLOAD_MB`` size limit is rejected with a specific,
  user-facing message, no dataset is created, and **the staged S3 object is
  deleted**. The rejection is raised as :class:`UploadInvalidError`, whose
  message the route returns as the ``422`` body.
- **7.6** — when the file has more usable rows than ``MAX_REVIEWS``, the preview
  says so (``usable_rows`` vs ``will_keep``) and says which rows will be kept:
  the most recent by date when a date column is mapped
  (``keep_rule="most_recent_by_date"``), otherwise the first rows in the file
  (``keep_rule="first_in_file"``).

Design note (dataset-ingestion ``ingestion.upload_parser`` bullet): the parser
"reads the object from S3, uses the ``csv`` sniffer, applies the header synonym
table …, counts usable rows, and applies the ``MAX_REVIEWS`` keep rule for the
preview." The preview shape is ``{columns, suggested_mapping, sample_rows,
usable_rows, will_keep, keep_rule}``.

The parsing itself is kept **pure and testable**: :func:`parse_bytes` turns raw
bytes into a :class:`UploadPreview` with no I/O, and :func:`preview_upload` is a
thin wrapper that reads the object from S3 and, on an invalid file, deletes it.
Both honour the engineering rules: S3 keys come only from
:mod:`app.storage.keys`, limits come from :mod:`app.core.config`, and nothing
depends on process memory.

This module does **not** create a dataset — that is task 8.3
(``service.create_from_upload``). Its job is parsing, preview building, and the
delete-on-invalid cleanup.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.storage import keys, s3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header synonym table
# ---------------------------------------------------------------------------

#: The mappable fields. ``text`` is required (Requirement 7.2); the rest are
#: optional. Order is the natural reading order used for a stable mapping.
TEXT = "text"
RATING = "rating"
DATE = "date"
AUTHOR = "author"
TITLE = "title"

#: Common header names → canonical field. Matching is case-insensitive and
#: ignores surrounding whitespace (see :func:`_normalize_header`). The design
#: lists ``review|text|comment|body`` → text and ``rating|stars|score`` →
#: rating as examples; the rest follow the optional columns in Requirement 7.2.
#:
#: Longer/more specific synonyms are listed so the first match wins per column;
#: a header that matches no synonym stays unmapped and is shown to the analyst
#: to map by hand.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    TEXT: ("review", "text", "comment", "body", "review text", "content", "feedback"),
    RATING: ("rating", "stars", "score", "star rating", "rate"),
    DATE: ("date", "review date", "created", "created at", "timestamp", "posted", "time"),
    AUTHOR: ("author", "name", "reviewer", "user", "username", "customer"),
    TITLE: ("title", "subject", "headline", "summary"),
}

#: Keep-rule labels used in the preview and (later) ``mapping.json``.
KEEP_MOST_RECENT = "most_recent_by_date"
KEEP_FIRST = "first_in_file"

#: How many parsed rows to include as a sample in the preview.
_SAMPLE_ROWS = 3


# ---------------------------------------------------------------------------
# Errors and value objects
# ---------------------------------------------------------------------------


class UploadInvalidError(Exception):
    """A staged upload cannot become a dataset (Requirement 7.3).

    Raised when the file cannot be parsed, has no usable review-text column, or
    exceeds the size limit. The ``message`` is user-facing and is returned as
    the ``422`` body by the preview route; :func:`preview_upload` deletes the
    staged S3 object before this propagates, so an invalid upload never leaves
    an orphan behind.
    """


@dataclass(frozen=True)
class UploadPreview:
    """The preview the UI shows before an upload is submitted.

    Mirrors the design's ``POST /uploads/{upload_id}/preview`` response shape
    ``{columns, suggested_mapping, sample_rows, usable_rows, will_keep,
    keep_rule}``.

    Attributes:
        columns: The file's header names, in file order.
        suggested_mapping: Canonical field → detected header name. Always
            contains ``text`` (the file would be rejected otherwise); optional
            fields appear only when a matching header was found.
        sample_rows: A few parsed rows (header name → cell value) for display.
        usable_rows: Count of rows with non-empty review text.
        will_keep: How many rows survive the ``MAX_REVIEWS`` keep rule
            (``min(usable_rows, MAX_REVIEWS)``).
        keep_rule: ``most_recent_by_date`` when a date column is mapped, else
            ``first_in_file`` (Requirement 7.6).
    """

    columns: list[str]
    suggested_mapping: dict[str, str]
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    usable_rows: int = 0
    will_keep: int = 0
    keep_rule: str = KEEP_FIRST

    def to_dict(self) -> dict[str, object]:
        """Return the JSON body for the preview endpoint."""
        return {
            "columns": self.columns,
            "suggested_mapping": self.suggested_mapping,
            "sample_rows": self.sample_rows,
            "usable_rows": self.usable_rows,
            "will_keep": self.will_keep,
            "keep_rule": self.keep_rule,
        }


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def _normalize_header(name: str) -> str:
    """Lower-case and trim a header for case-insensitive synonym matching."""
    return name.strip().lower()


def keep_rule_for(mapping: dict[str, str]) -> str:
    """Return the keep rule implied by a column *mapping* (Requirement 7.6).

    The keep rule is a pure function of whether a **date** column is mapped:
    ``most_recent_by_date`` when ``"date"`` is a key in *mapping*, else
    ``first_in_file``. Keeping it here means every caller — the parser's own
    preview (over the *auto-suggested* mapping) and
    :func:`app.ingestion.service.create_from_upload` (over the analyst's
    *confirmed* mapping) — derives the rule the same way from whatever mapping
    it holds, so a date column the synonym table does not auto-detect (e.g. a
    header named ``when``) still yields ``most_recent_by_date`` once the analyst
    maps it.

    Args:
        mapping: A canonical field → header-name mapping (suggested or
            confirmed). Only the presence of the ``"date"`` key matters.

    Returns:
        :data:`KEEP_MOST_RECENT` when a date column is mapped, else
        :data:`KEEP_FIRST`.
    """
    return KEEP_MOST_RECENT if DATE in mapping else KEEP_FIRST


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Map canonical fields to the file's header names using :data:`_SYNONYMS`.

    Case-insensitive: a header matches a synonym when its trimmed, lower-cased
    form equals the synonym. Each field takes the **first** header (in file
    order) that matches one of its synonyms, and each header is claimed by at
    most one field, so a file whose columns are ``review, rating, date`` maps
    cleanly without one header being used for two fields.

    Args:
        columns: The file's header names in file order.

    Returns:
        Canonical field → header name, for every field a header was found for.
        ``text`` may be absent here; the caller treats its absence as an invalid
        file (Requirement 7.3).
    """
    normalized = [(col, _normalize_header(col)) for col in columns]
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for field_name, synonyms in _SYNONYMS.items():
        synonym_set = set(synonyms)
        for original, norm in normalized:
            if original in claimed:
                continue
            if norm in synonym_set:
                mapping[field_name] = original
                claimed.add(original)
                break
    return mapping


def _sniff_delimiter(sample: str) -> str:
    """Return the delimiter for *sample*, supporting CSV and TSV.

    Uses :class:`csv.Sniffer` restricted to comma and tab so a comma-rich TSV
    (or a tab-rich CSV) is not misread. Falls back to a comma when the sniffer
    cannot decide (for example a single column with no delimiter), which still
    parses a one-column file correctly.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t").delimiter
    except csv.Error:
        return ","


def parse_bytes(raw: bytes, *, max_reviews: int) -> UploadPreview:
    """Parse upload *raw* bytes into a :class:`UploadPreview` (no I/O).

    Steps (Requirements 7.2, 7.6):

    1. Decode as UTF-8 (BOM-tolerant); an undecodable file is unparseable.
    2. Sniff the delimiter (comma or tab) from the first part of the file.
    3. Read the header row and parse the data rows.
    4. Suggest a mapping from the headers via :func:`suggest_mapping`; a missing
       ``text`` column means the file is invalid (Requirement 7.3).
    5. Count usable rows (non-empty review text in the mapped text column).
    6. Choose the keep rule: ``most_recent_by_date`` when a date column is
       mapped, else ``first_in_file``; ``will_keep`` is ``min(usable_rows,
       max_reviews)`` (Requirement 7.6).

    Args:
        raw: The raw file bytes read from S3.
        max_reviews: ``MAX_REVIEWS`` — the keep-rule cap.

    Returns:
        The preview for a valid file.

    Raises:
        UploadInvalidError: The file cannot be decoded or parsed, has no header
            row, or has no usable review-text column. The message is
            user-facing (Requirement 7.3).
    """
    # 1. Decode (BOM-tolerant). A binary / wrong-encoding file is unparseable.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadInvalidError(
            "The file could not be read as text. Upload a UTF-8 encoded .csv or .tsv file."
        ) from exc

    if not text.strip():
        raise UploadInvalidError("The file is empty.")

    # 2. Sniff the delimiter from a leading sample.
    delimiter = _sniff_delimiter(text[:8192])

    # 3. Parse. The first row is the header.
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise UploadInvalidError("The file could not be parsed as CSV or TSV.") from exc

    if not rows:
        raise UploadInvalidError("The file has no header row.")

    header = [h.strip() for h in rows[0]]
    if not any(header):
        raise UploadInvalidError("The file has no header row.")

    data_rows = rows[1:]

    # 4. Suggest a mapping; a missing text column is a hard rejection (7.3).
    mapping = suggest_mapping(header)
    if TEXT not in mapping:
        raise UploadInvalidError(
            "No review text column was found. The file needs a column such as "
            "'review', 'text', 'comment', or 'body'."
        )

    text_index = header.index(mapping[TEXT])

    # 5. Count usable rows (non-empty review text).
    usable_rows = 0
    for row in data_rows:
        if text_index < len(row) and row[text_index].strip():
            usable_rows += 1

    # 6. Keep rule + will_keep (Requirement 7.6).
    keep_rule = keep_rule_for(mapping)
    will_keep = min(usable_rows, max_reviews)

    # Sample rows for display: the first few parsed rows as header→value dicts.
    sample_rows: list[dict[str, str]] = []
    for row in data_rows[:_SAMPLE_ROWS]:
        sample_rows.append(
            {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
        )

    return UploadPreview(
        columns=header,
        suggested_mapping=mapping,
        sample_rows=sample_rows,
        usable_rows=usable_rows,
        will_keep=will_keep,
        keep_rule=keep_rule,
    )


# ---------------------------------------------------------------------------
# S3-backed wrapper
# ---------------------------------------------------------------------------


def preview_upload(upload_id: str) -> UploadPreview:
    """Read the staged upload from S3 and build its preview.

    Reads ``uploads/{upload_id}/file`` (key from
    :func:`app.storage.keys.upload_file`), enforces the ``MAX_UPLOAD_MB`` size
    limit, and parses the bytes with :func:`parse_bytes` using ``MAX_REVIEWS``
    from configuration. On any invalid-file condition — over the size limit,
    unparseable, or no usable text column — the staged object is **deleted** and
    :class:`UploadInvalidError` is re-raised so the route returns a ``422`` with
    the specific message (Requirement 7.3).

    Args:
        upload_id: The id returned by ``POST /uploads``; identifies the staged
            object.

    Returns:
        The :class:`UploadPreview` for a valid upload.

    Raises:
        UploadInvalidError: The staged file is over the size limit, unparseable,
            or has no usable review-text column. The staged object has been
            deleted before this is raised.
    """
    settings = get_settings()
    key = keys.upload_file(upload_id)

    raw = s3.get_bytes(key)

    # Size guard (Requirement 7.3). The pre-signed PUT already pins
    # Content-Length, but re-check here so a parser caller is self-contained.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(raw) > max_bytes:
        s3.delete_object(key)
        raise UploadInvalidError(
            f"The file is larger than the {settings.max_upload_mb} MB upload limit."
        )

    try:
        return parse_bytes(raw, max_reviews=settings.max_reviews)
    except UploadInvalidError:
        # Reject → delete the staged object so no orphan is left (Requirement 7.3).
        s3.delete_object(key)
        raise
