"""Property-based test for the "one dataset per URL" guarantee (11).

- **Property 6: One dataset per URL.** *For any* interleaving of concurrent Add
  requests for URLs with the same normalized form, exactly one dataset SHALL
  exist for that URL afterward. Validates: Requirements 6.2, 6.4, 6.7

The *true* concurrency guarantee needs PostgreSQL — two real Adds racing the
partial unique index ``uq_datasets_normalized_url`` — and is proved by the
integration test ``tests/integration/ingestion/test_add_service_int.py``
(concurrent-duplicate case). This property test covers the complementary
**logical** invariant across a wide input space with Hypothesis, exactly as
``test_refresh_version.py`` does for Property 7: a shared in-memory model of
the datasets table in which the three real defences combine —

1. the duplicate lookup at Check time (``existing_dataset`` set when a dataset
   already tracks the normalized URL),
2. the conditional ``applied`` marking (a double-clicked / second Add of the
   same *item* loses the claim and creates nothing), and
3. the unique-index race fallback (two Adds of the same *new* normalized URL:
   one INSERT wins, the loser's INSERT raises ``IntegrityError`` on the unique
   index and ``create_from_check`` falls back to refreshing the winner).

The model drives the **real** ``add_items`` / ``create_from_check`` against a
``_Table`` that enforces the unique index just like PostgreSQL, so the
service's own race-handling code decides the outcome. For any interleaving of
Adds for one normalized URL, the table holds exactly one row for it afterward.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from app.datasets.refresh_service import CheckCapture
from app.ingestion import service
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.duplicates import ExistingDataset
from app.ingestion.service import AddRequestItem
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.exc import IntegrityError

_CHECK_ID = "chk-prop6"
_NORMALIZED = "https://acme.example/reviews"
_UNIQUE_INDEX = "uq_datasets_normalized_url"


def _integrity_error() -> IntegrityError:
    """A unique violation on the normalized-URL index, as the DB would raise."""
    orig = Exception(
        f'duplicate key value violates unique constraint "{_UNIQUE_INDEX}"\n'
        f"DETAIL:  Key (normalized_url)=({_NORMALIZED}) already exists."
    )
    return IntegrityError("INSERT INTO datasets ...", {}, orig)


class _Table:
    """A tiny in-memory model of the datasets table's unique-URL index.

    ``insert`` enforces the partial unique index on ``normalized_url`` exactly
    like PostgreSQL: a second insert of the same normalized URL raises the same
    ``IntegrityError`` the real index produces, so ``create_from_check``'s race
    fallback runs for real. ``find`` is the duplicate lookup.
    """

    def __init__(self) -> None:
        self.rows_by_norm: dict[str, str] = {}  # normalized_url -> dataset_id
        self.refreshes: list[str] = []  # dataset_ids refreshed

    def insert(self, dataset_id: str, normalized: str) -> None:
        if normalized in self.rows_by_norm:
            raise _integrity_error()
        self.rows_by_norm[normalized] = dataset_id

    def find(self, normalized: str) -> ExistingDataset | None:
        ds_id = self.rows_by_norm.get(normalized)
        if ds_id is None:
            return None
        return ExistingDataset(id=ds_id, name="Acme", archived=False, status="updated")

    def refresh(self, dataset_id: str, trigger: str, capture: CheckCapture) -> str:
        self.refreshes.append(dataset_id)
        return "refreshed"


def _item(item_id: str, *, tracked_id: str | None) -> CheckItem:
    """A will_work item for the shared normalized URL.

    ``tracked_id`` set models the duplicate lookup at Check time having already
    found an existing dataset (the "already tracked" branch, Requirement 6.4).
    """
    existing = (
        {"id": tracked_id, "name": "Acme", "archived": False, "status": "updated"}
        if tracked_id
        else None
    )
    return CheckItem(
        item_id=item_id,
        input=_NORMALIZED,
        state="done",  # type: ignore[arg-type]
        normalized=_NORMALIZED,
        final_url=_NORMALIZED,
        hops=[{"url": _NORMALIZED, "status": 200, "timestamp": "t"}],
        verdict={
            "verdict": "will_work",
            "reasons": ["r"],
            "warnings": [],
            "evidence": {
                "reviews_verified": 9,
                "method": "selectors",
                "page_title": "T",
                "samples": [],
            },
        },
        existing_dataset=existing,
        capture_prefix=f"checks/{_CHECK_ID}/{item_id}/",
    )


class _SessionStore:
    """Offline check_session stand-in with a per-item one-shot `applied` claim."""

    def __init__(self, session: CheckSession) -> None:
        self._session = session
        self._claimed: set[str] = set()

    def get_session(self, check_id: str) -> CheckSession | None:
        return self._session

    def set_item_fields(
        self,
        check_id: str,
        item_id: str,
        fields: dict[str, Any],
        *,
        expected_state: str | None = None,
    ) -> bool:
        # Conditional done→applied: only the first claimant of an item wins,
        # modelling the DynamoDB conditional write under concurrency.
        if expected_state == "done" and item_id in self._claimed:
            return False
        self._claimed.add(item_id)
        item = self._session.items.get(item_id)
        if item is not None and "state" in fields:
            item.state = fields["state"]  # type: ignore[assignment]
        return True


@contextmanager
def _patched(store: _SessionStore, table: _Table) -> Any:
    def _insert_dataset(dataset_id: str, item: CheckItem) -> None:
        table.insert(dataset_id, item.normalized)

    with (
        patch.object(service, "check_session", store),
        patch.object(service.s3, "copy_object", lambda src, dst: None),
        patch.object(service.s3, "object_exists", lambda key: False),
        patch.object(service.s3, "delete_object", lambda key: None),
        patch.object(service, "enqueue", lambda *a, **k: None),
        patch.object(service, "_insert_dataset", _insert_dataset),
        patch.object(service.refresh_service, "refresh", table.refresh),
        patch.object(service.duplicates, "find_existing", lambda n, f: table.find(n or "")),
        patch.object(
            service,
            "get_settings",
            lambda: type("S", (), {"processing_queue_url": "q"})(),
        ),
    ):
        yield


@given(
    n_new=st.integers(min_value=0, max_value=4),
    n_dup_item=st.integers(min_value=0, max_value=3),
    pre_tracked=st.booleans(),
)
def test_exactly_one_dataset_per_normalized_url(
    n_new: int, n_dup_item: int, pre_tracked: bool
) -> None:
    """Property 6: One dataset per URL.

    For any interleaving of concurrent Add requests for URLs with the same
    normalized form, exactly one dataset exists for that URL afterward.
    Validates: Requirements 6.2, 6.4, 6.7

    The interleaving is modelled by replaying, against one shared ``_Table``,
    a mix of:
      * ``n_new`` Adds of the *same new* normalized URL via distinct items (the
        unique-index race: one wins the INSERT, the rest fall back to refresh);
      * ``n_dup_item`` repeated Adds of a *single* item id (the conditional
        ``applied`` claim: only the first does anything);
      * optionally a pre-existing tracked dataset for the URL (the duplicate
        lookup routes every Add straight to refresh).
    """
    table = _Table()

    # Optionally seed a pre-existing dataset for this normalized URL.
    pre_id = "00000000-0000-0000-0000-0000000000aa"
    if pre_tracked:
        table.rows_by_norm[_NORMALIZED] = pre_id

    # Distinct items for the "same new URL" race, each its own item id.
    new_items = [_item(f"n{i}", tracked_id=pre_id if pre_tracked else None) for i in range(n_new)]
    # One item repeatedly Added (double-click / second worker on the same item).
    dup_item = _item("dup", tracked_id=pre_id if pre_tracked else None)

    all_items = new_items + ([dup_item] if n_dup_item else [])
    if not all_items:
        return  # nothing requested; trivially satisfied

    session = CheckSession(
        check_id=_CHECK_ID,
        created_at="2024-01-01T00:00:00+00:00",
        ttl=9_999_999_999,
        origin="new",  # type: ignore[arg-type]
        items={it.item_id: it for it in all_items},
    )
    store = _SessionStore(session)

    requests = [AddRequestItem(it.item_id) for it in new_items]
    requests += [AddRequestItem("dup") for _ in range(n_dup_item)]

    with _patched(store, table):
        service.add_items(_CHECK_ID, requests)

    # The core invariant: exactly one dataset row exists for the normalized URL.
    assert len(table.rows_by_norm) <= 1
    if all_items:
        assert _NORMALIZED in table.rows_by_norm
        assert len(table.rows_by_norm) == 1
