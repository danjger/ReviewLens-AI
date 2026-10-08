"""Unit tests for app.ingestion.url_validator.parse_submission.

Covers Requirements 1.1 (up to 10 lines), 1.2 (invalid-line marking while
still processing the rest), and 1.4 (collapsing duplicates within a batch
after normalization).
"""

from __future__ import annotations

from app.ingestion.url_validator import parse_submission


def test_blank_lines_skipped() -> None:
    items = parse_submission("\n\nhttps://a.com\n\n")
    assert [i.input for i in items] == ["https://a.com"]


def test_ten_line_limit() -> None:
    raw = "\n".join(f"https://example.com/p{i}" for i in range(15))
    items = parse_submission(raw, max_urls=10)
    assert len(items) == 10
    assert items[0].item_id == "u1"
    assert items[-1].item_id == "u10"


def test_invalid_lines_marked_but_others_processed() -> None:
    raw = "not a url\nftp://example.com\nhttps://good.com/p"
    items = parse_submission(raw)
    assert items[0].state == "invalid"
    assert items[0].message is not None
    # ftp is not http/https → invalid
    assert items[1].state == "invalid"
    # the valid line is still checked
    assert items[2].state == "pending"
    assert items[2].normalized == "https://good.com/p"


def test_missing_host_is_invalid() -> None:
    items = parse_submission("https:///path-only")
    assert items[0].state == "invalid"


def test_duplicates_collapsed_after_normalization() -> None:
    raw = (
        "https://example.com/p\n"
        "https://www.example.com/p/\n"  # same after normalization
        "https://example.com/p?utm_source=x\n"  # same after normalization
        "https://example.com/other"
    )
    items = parse_submission(raw)
    assert items[0].state == "pending"
    assert items[1].state == "duplicate_in_batch"
    assert items[2].state == "duplicate_in_batch"
    assert items[3].state == "pending"
    # duplicate message points at the first occurrence
    assert items[1].message is not None
    assert "u1" in items[1].message


def test_first_occurrence_is_pending() -> None:
    raw = "https://example.com/p\nhttps://example.com/p"
    items = parse_submission(raw)
    assert items[0].state == "pending"
    assert items[1].state == "duplicate_in_batch"


def test_invalid_lines_do_not_count_as_duplicates() -> None:
    raw = "bad\nbad"
    items = parse_submission(raw)
    assert [i.state for i in items] == ["invalid", "invalid"]
