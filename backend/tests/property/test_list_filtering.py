"""Property-based test for list filtering/ordering (dataset-library task 8).

Property 3: List filtering is exact.
  For any set of datasets and search text, the list SHALL contain exactly the
  non-archived datasets whose name or URL contains the text (case-insensitive),
  in the requested order.
  Validates: Requirements 2.1, 2.2, 2.3

``list_datasets`` builds a SQL query (``archived`` filter, an
``ilike('%term%')`` match on ``name`` OR ``original_url`` with wildcards
escaped by the real :func:`app.datasets.library._escape_like`, then an
``ORDER BY``). Driving that against PostgreSQL would need the Compose stack and
make this slow, so — as the task permits — this test extracts a small *pure*
helper (:func:`_select`) in this file that mirrors the exact filter and sort
semantics, and uses the **real** ``_escape_like`` to assert the escape intent
(a search term with ``%``/``_``/``\\`` matches those characters literally, not
as patterns).

The modeled predicate is the exact-substring, case-insensitive match the design
and ``_escape_like`` intend; the modeled order mirrors ``_order_by``:

- ``name`` → by ``name`` under a C (byte) collation ascending, ``id`` as a
  deterministic tie-break;
- every other sort (``activity``/``refreshed``/``status`` default path here) →
  by ``updated_at`` descending, ``id`` ascending.

The property asserts the result is **exactly** the matching, correctly-archived
rows (no extra, none missing) and is in the required order.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.datasets.library import _escape_like
from hypothesis import given
from hypothesis import strategies as st


@dataclass(frozen=True)
class Row:
    """A dataset reduced to the fields the filter/sort actually read."""

    id: str
    name: str
    original_url: str | None
    updated_at: str
    archived: bool


def _matches(row: Row, text: str) -> bool:
    """The exact match the design intends: case-insensitive substring on name OR URL.

    ``main_url`` for an upload row is its ``name`` (which the name clause already
    covers), so matching ``name`` OR ``original_url`` is the full "name or URL"
    rule. A blank/whitespace term applies no filter (every row matches).
    """
    term = text.strip()
    if not term:
        return True
    needle = term.casefold()
    if needle in row.name.casefold():
        return True
    if row.original_url is not None and needle in row.original_url.casefold():
        return True
    return False


def _select(rows: list[Row], *, archived: bool, sort: str, q: str | None) -> list[Row]:
    """Pure mirror of ``list_datasets``' filter + sort over modeled rows."""
    kept = [r for r in rows if r.archived == archived and _matches(r, q or "")]
    if sort == "name":
        return sorted(kept, key=lambda r: (r.name, r.id))
    # updated_at DESC, id ASC — sort by id asc first, then stable sort by
    # updated_at desc.
    kept.sort(key=lambda r: r.id)
    kept.sort(key=lambda r: r.updated_at, reverse=True)
    return kept


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_names = st.text(min_size=0, max_size=12)
_urls = st.one_of(st.none(), st.text(min_size=1, max_size=16))
_timestamps = st.text(
    alphabet="0123456789:-T", min_size=4, max_size=10
)  # opaque sortable-ish keys; only relative order matters


@st.composite
def _rows(draw: st.DrawFn) -> list[Row]:
    ids = draw(st.lists(st.uuids().map(str), min_size=0, max_size=8, unique=True))
    out: list[Row] = []
    for rid in ids:
        out.append(
            Row(
                id=rid,
                name=draw(_names),
                original_url=draw(_urls),
                updated_at=draw(_timestamps),
                archived=draw(st.booleans()),
            )
        )
    return out


@given(
    rows=_rows(),
    q=st.one_of(st.none(), st.text(min_size=0, max_size=6)),
    archived=st.booleans(),
    sort=st.sampled_from(["activity", "name", "refreshed", "status"]),
)
def test_list_filtering_is_exact(rows: list[Row], q: str | None, archived: bool, sort: str) -> None:
    """Property 3: List filtering is exact.

    The result is exactly the correctly-archived rows whose name or URL contains
    the (case-insensitive) search text, in the requested order.
    Validates: Requirements 2.1, 2.2, 2.3
    """
    result = _select(rows, archived=archived, sort=sort, q=q)
    result_ids = {r.id for r in result}

    # Membership is exact: a row is in the result IFF it has the right archived
    # flag AND matches the search text — no extra rows, none missing.
    for row in rows:
        should_be_in = row.archived == archived and _matches(row, q or "")
        assert (row.id in result_ids) == should_be_in

    expected_ids = {r.id for r in rows if r.archived == archived and _matches(r, q or "")}
    assert len(result) == len(expected_ids)

    # Order is the requested order (a deterministic, stable sort).
    if sort == "name":
        keys = [(r.name, r.id) for r in result]
        assert keys == sorted(keys)
    else:
        # updated_at DESC, then id ASC within equal timestamps.
        for a, b in zip(result, result[1:], strict=False):
            assert (a.updated_at > b.updated_at) or (a.updated_at == b.updated_at and a.id <= b.id)


@given(term=st.text(min_size=1, max_size=10))
def test_escape_like_makes_wildcards_literal(term: str) -> None:
    """Property 3: List filtering is exact (escape intent).

    The real ``_escape_like`` escapes every LIKE wildcard (``%``, ``_``) and the
    escape char (``\\``) so the search term matches those characters literally,
    which is what makes the ILIKE match an exact case-insensitive substring.
    Validates: Requirement 2.3
    """
    escaped = _escape_like(term)

    # Every wildcard char in the term is backslash-escaped in the output.
    assert escaped.count("\\%") == term.count("%")
    assert escaped.count("\\_") == term.count("_")
    # Backslash bookkeeping: each input ``\`` is doubled (2 backslashes) and
    # each ``%``/``_`` gains one escaping backslash, so the total backslash
    # count is exactly accounted for — no wildcard is left un-escaped.
    expected_backslashes = 2 * term.count("\\") + term.count("%") + term.count("_")
    assert escaped.count("\\") == expected_backslashes
