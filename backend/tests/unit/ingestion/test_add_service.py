"""Unit tests for app.ingestion.service.add_items / create_from_check (task 7.1).

These exercise the Add step's *orchestration* with every heavy dependency —
the Check Session store (DynamoDB), S3, the Refresh Service, the processing
queue, and the dataset INSERT — faked at the module boundary, so the tests are
fast and fully offline. They cover the branches the task calls out:

- ``wont_work`` is refused: no dataset/version is created (Requirement 3.8,
  Correctness Property 5);
- ``limited`` without ``confirm_limited`` needs confirmation and creates
  nothing (Requirement 3.9); with confirmation it is created;
- a new, viable, not-tracked URL creates a v1 dataset: capture copied, row
  inserted with the viability verdict and redirects in ``status_detail``, a v1
  ``dataset_versions`` row, and a processing message enqueued (Requirements
  5.1–5.3, 3.11, 2.5);
- an already-tracked URL routes to ``refresh_service.refresh`` with trigger
  ``duplicate_submission`` and no new dataset (Requirement 6.4);
- the conditional ``applied`` marking stops a double-clicked / concurrent Add
  from creating two datasets (design Error Handling);
- an expired session yields ``expired`` for each item (Requirement 5.4);
- a copy/insert failure deletes the partial objects and creates no record
  (Requirement 4.5).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from app.datasets.refresh_service import CheckCapture
from app.ingestion import service
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.duplicates import ExistingDataset
from app.ingestion.service import AddRequestItem
from app.storage import keys
from sqlalchemy.exc import IntegrityError

_CHECK_ID = "chk-1"
_EXISTING_ID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _verdict(label: str, *, page_title: str = "Acme CRM Reviews") -> dict[str, Any]:
    """A minimal stored verdict dict (shape of Verdict.to_dict())."""
    return {
        "verdict": label,
        "reasons": ["24 reviews found and verified on this page"],
        "warnings": [],
        "evidence": {
            "reviews_verified": 24,
            "method": "selectors",
            "page_title": page_title,
            "samples": [],
        },
    }


def _item(
    *,
    item_id: str = "u1",
    state: str = "done",
    verdict_label: str | None = "will_work",
    existing: dict[str, Any] | None = None,
    final_url: str | None = "https://acme.example/reviews",
) -> CheckItem:
    return CheckItem(
        item_id=item_id,
        input="https://acme.example/reviews",
        state=state,  # type: ignore[arg-type]
        normalized="https://acme.example/reviews",
        final_url=final_url,
        hops=[{"url": "https://acme.example/reviews", "status": 200, "timestamp": "t"}],
        verdict=_verdict(verdict_label) if verdict_label else None,
        existing_dataset=existing,
        capture_prefix=f"checks/{_CHECK_ID}/{item_id}/",
    )


def _session(items: list[CheckItem], origin: str = "new") -> CheckSession:
    return CheckSession(
        check_id=_CHECK_ID,
        created_at="2024-01-01T00:00:00+00:00",
        ttl=9_999_999_999,
        origin=origin,  # type: ignore[arg-type]
        items={it.item_id: it for it in items},
    )


class _FakeSessionStore:
    """Stand-in for app.ingestion.check_session used by the service."""

    def __init__(self, session: CheckSession | None, *, claim: bool = True) -> None:
        self._session = session
        self._claim = claim
        self.field_writes: list[dict[str, Any]] = []

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
        self.field_writes.append(
            {"item_id": item_id, "fields": fields, "expected_state": expected_state}
        )
        # Reflect the applied state onto the in-memory item so a later read in
        # the same test sees it, mirroring DynamoDB.
        if self._claim and self._session is not None:
            item = self._session.items.get(item_id)
            if item is not None and "state" in fields:
                item.state = fields["state"]  # type: ignore[assignment]
        return self._claim


class _Recorder:
    """Captures S3, refresh, enqueue, and dataset-insert calls."""

    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.existing_objects: set[str] = set()
        self.enqueues: list[tuple[str, dict[str, Any], str | None]] = []
        self.inserts: list[tuple[str, CheckItem]] = []
        self.refreshes: list[tuple[str, str, CheckCapture]] = []
        self.refresh_outcome: str = "refreshed"
        self.insert_error: Exception | None = None

    # S3
    def copy_object(self, src: str, dst: str) -> None:
        self.copies.append((src, dst))

    def object_exists(self, key: str) -> bool:
        return key in self.existing_objects

    def delete_object(self, key: str) -> None:
        self.deletes.append(key)

    # queue
    def enqueue(self, queue_url: str, body: dict[str, Any], **kw: Any) -> None:
        self.enqueues.append((queue_url, body, kw.get("message_group_id")))

    # dataset insert
    def insert_dataset(self, dataset_id: str, item: CheckItem) -> None:
        if self.insert_error is not None:
            raise self.insert_error
        self.inserts.append((dataset_id, item))

    # refresh service
    def refresh(self, dataset_id: str, trigger: str, capture: CheckCapture) -> str:
        self.refreshes.append((dataset_id, trigger, capture))
        return self.refresh_outcome


@contextmanager
def _patched(store: _FakeSessionStore, rec: _Recorder) -> Any:
    with (
        patch.object(service, "check_session", store),
        patch.object(service.s3, "copy_object", rec.copy_object),
        patch.object(service.s3, "object_exists", rec.object_exists),
        patch.object(service.s3, "delete_object", rec.delete_object),
        patch.object(service, "enqueue", rec.enqueue),
        patch.object(service, "_insert_dataset", rec.insert_dataset),
        patch.object(service.refresh_service, "refresh", rec.refresh),
        patch.object(
            service,
            "get_settings",
            lambda: type("S", (), {"processing_queue_url": "proc-queue-url"})(),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# wont_work refused (Requirement 3.8, Property 5)
# ---------------------------------------------------------------------------


def test_wont_work_is_refused_and_creates_nothing() -> None:
    store = _FakeSessionStore(_session([_item(verdict_label="wont_work")]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert len(results) == 1
    assert results[0].outcome == "refused_wont_work"
    assert results[0].dataset_id is None
    # No applied marking, no copy, no insert, no enqueue, no refresh.
    assert store.field_writes == []
    assert rec.copies == []
    assert rec.inserts == []
    assert rec.enqueues == []
    assert rec.refreshes == []


# ---------------------------------------------------------------------------
# limited needs confirmation (Requirement 3.9)
# ---------------------------------------------------------------------------


def test_limited_without_confirmation_needs_confirmation() -> None:
    store = _FakeSessionStore(_session([_item(verdict_label="limited")]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1", confirm_limited=False)])

    assert results[0].outcome == "needs_confirmation"
    assert store.field_writes == []  # not even claimed
    assert rec.inserts == []
    assert rec.enqueues == []


def test_limited_with_confirmation_is_created() -> None:
    store = _FakeSessionStore(_session([_item(verdict_label="limited")]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1", confirm_limited=True)])

    assert results[0].outcome == "created"
    assert results[0].dataset_id is not None
    assert len(rec.inserts) == 1


# ---------------------------------------------------------------------------
# new dataset created: copy + insert + enqueue (Requirements 5.1–5.3)
# ---------------------------------------------------------------------------


def test_new_url_creates_dataset_with_v1_copy_and_enqueue() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert results[0].outcome == "created"
    dataset_id = results[0].dataset_id
    assert dataset_id is not None

    # applied marking happened (done → applied).
    assert store.field_writes[0]["fields"] == {"state": "applied"}
    assert store.field_writes[0]["expected_state"] == "done"

    # page + plan copied into v1 destinations from storage.keys.
    assert (keys.check_page(_CHECK_ID, "u1"), keys.dataset_raw_page(dataset_id, 1, 1)) in rec.copies
    assert (keys.check_plan(_CHECK_ID, "u1"), keys.dataset_raw_plan(dataset_id, 1)) in rec.copies

    # one dataset insert, and a FIFO processing message with IDs only.
    assert len(rec.inserts) == 1
    assert rec.inserts[0][0] == dataset_id
    assert rec.enqueues == [
        ("proc-queue-url", {"dataset_id": dataset_id, "data_version": 1}, dataset_id)
    ]
    # not routed to refresh.
    assert rec.refreshes == []


def test_new_url_copies_snapshot_when_present() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.existing_objects.add(keys.check_snapshot(_CHECK_ID, "u1"))

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    dataset_id = results[0].dataset_id
    assert dataset_id is not None
    assert (
        keys.check_snapshot(_CHECK_ID, "u1"),
        keys.dataset_snapshot(dataset_id, 1),
    ) in rec.copies


def test_new_url_skips_absent_snapshot() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()  # snapshot not registered as existing

    with _patched(store, rec):
        service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert [dst for _, dst in rec.copies if "snapshot" in dst] == []


# ---------------------------------------------------------------------------
# viability + redirects in status_detail, v1 row (Requirement 3.11, 5.2)
# ---------------------------------------------------------------------------


def test_insert_dataset_writes_viability_and_version_row() -> None:
    """create_from_check inserts the row with viability + redirects + v1 row.

    This drives the *real* _insert_dataset against a fake session_scope that
    records the two INSERT statements, so we assert the status_detail payload
    and the v1 dataset_versions row without a database.
    """
    item = _item(verdict_label="will_work")
    executed: list[tuple[str, dict[str, Any]]] = []

    class _FakeDbSession:
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
            sql = " ".join(str(statement).split())
            bind = {name: bp.value for name, bp in getattr(statement, "_bindparams", {}).items()}
            executed.append((sql, bind))
            return None

    @contextmanager
    def _scope() -> Any:
        yield _FakeDbSession()

    with patch.object(service, "session_scope", _scope):
        service._insert_dataset("ds-123", item)

    dataset_sql, dataset_bind = executed[0]
    assert "INSERT INTO datasets" in dataset_sql
    assert dataset_bind["status"] == "requested"
    assert dataset_bind["source_type"] == "url"
    assert dataset_bind["original_url"] == item.input
    # viability verdict + redirects + requested event live in status_detail.
    import json as _json

    detail = _json.loads(dataset_bind["status_detail"])
    assert detail["viability"]["verdict"] == "will_work"
    assert detail["redirects"] == item.hops
    assert detail["events"][0]["status"] == "requested"
    assert detail["events"][0]["data"]["viability"]["verdict"] == "will_work"
    assert detail["events"][0]["data"]["redirects"] == item.hops

    version_sql, version_bind = executed[1]
    assert "INSERT INTO dataset_versions" in version_sql
    # version 1 literal; the dataset_id bind is cast to uuid so the native
    # ``uuid`` column is written with the correct type (not VARCHAR).
    assert "VALUES (CAST(:id AS uuid), 1, :trigger" in version_sql
    assert version_bind["trigger"] == "initial"


# ---------------------------------------------------------------------------
# tracked URL routes to refresh (Requirement 6.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("refresh_outcome", "expected"),
    [
        ("refreshed", "refreshed"),
        ("restored_and_refreshed", "restored_and_refreshed"),
        ("already_refreshing", "already_refreshing"),
    ],
)
def test_tracked_url_routes_to_refresh(refresh_outcome: str, expected: str) -> None:
    existing = {"id": _EXISTING_ID, "name": "Acme CRM", "archived": False, "status": "updated"}
    item = _item(verdict_label="will_work", existing=existing)
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.refresh_outcome = refresh_outcome

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert results[0].outcome == expected
    assert results[0].dataset_id == _EXISTING_ID
    # routed to refresh with the duplicate_submission trigger; no new dataset.
    assert len(rec.refreshes) == 1
    dataset_id, trigger, capture = rec.refreshes[0]
    assert dataset_id == _EXISTING_ID
    assert trigger == "duplicate_submission"
    assert capture.page_key == keys.check_page(_CHECK_ID, "u1")
    assert rec.inserts == []
    assert rec.copies == []  # the Refresh Service does its own copying


# ---------------------------------------------------------------------------
# conditional applied marking prevents a double create (design Error Handling)
# ---------------------------------------------------------------------------


def test_lost_applied_claim_does_not_create_second_dataset() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]), claim=False)  # claim always loses
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    # The loser creates nothing and reports the in-flight/created state.
    assert results[0].outcome == "created"
    assert rec.inserts == []
    assert rec.copies == []
    assert rec.enqueues == []


def test_already_applied_item_is_idempotent() -> None:
    item = _item(verdict_label="will_work", state="applied")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert results[0].outcome == "created"
    # No re-claim, no re-create.
    assert store.field_writes == []
    assert rec.inserts == []


def test_already_applied_tracked_item_reports_already_refreshing() -> None:
    existing = {"id": _EXISTING_ID, "name": "Acme CRM", "archived": False, "status": "updated"}
    item = _item(verdict_label="will_work", state="applied", existing=existing)
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert results[0].outcome == "already_refreshing"
    assert rec.refreshes == []


# ---------------------------------------------------------------------------
# expired session (Requirement 5.4)
# ---------------------------------------------------------------------------


def test_expired_session_returns_expired_for_each_item() -> None:
    store = _FakeSessionStore(None)  # session gone
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1"), AddRequestItem("u2")])

    assert [r.outcome for r in results] == ["expired", "expired"]
    assert rec.inserts == []
    assert rec.copies == []


# ---------------------------------------------------------------------------
# cleanup on failure (Requirement 4.5)
# ---------------------------------------------------------------------------


def test_insert_failure_deletes_partial_objects_and_does_not_create() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.existing_objects.add(keys.check_snapshot(_CHECK_ID, "u1"))  # snapshot copied too
    rec.insert_error = RuntimeError("insert blew up")

    with _patched(store, rec):
        with pytest.raises(RuntimeError, match="insert blew up"):
            service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    # every copied permanent object was deleted; nothing enqueued.
    copied_dsts = {dst for _, dst in rec.copies}
    assert set(rec.deletes) == copied_dsts
    assert len(rec.deletes) == 3  # page, plan, snapshot
    assert rec.enqueues == []


def test_copy_failure_deletes_partial_objects() -> None:
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()

    calls: list[str] = []

    def _copy(src: str, dst: str) -> None:
        calls.append(dst)
        if "plan" in dst:
            raise RuntimeError("copy plan failed")
        rec.copies.append((src, dst))

    with _patched(store, rec), patch.object(service.s3, "copy_object", _copy):
        with pytest.raises(RuntimeError, match="copy plan failed"):
            service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    # the one object that was copied before the failure (the page) is deleted.
    assert rec.deletes == [dst for _, dst in rec.copies]
    assert len(rec.deletes) == 1
    assert rec.enqueues == []


# ---------------------------------------------------------------------------
# unique-constraint race fallback (task 7.2, Requirement 6.7, Property 6)
# ---------------------------------------------------------------------------


def _integrity_error(constraint: str) -> IntegrityError:
    """Build an IntegrityError whose text names *constraint*.

    Mirrors the message both psycopg (local) and the Data API (AWS) surface for
    a unique violation: ``...violates unique constraint "<name>"``. The service
    distinguishes the duplicate-URL race from other integrity errors by this
    name, so the message content — not the SQLAlchemy wrapper — is what matters.
    """
    orig = Exception(
        f'duplicate key value violates unique constraint "{constraint}"\n'
        "DETAIL:  Key (normalized_url)=(https://acme.example/reviews) already exists."
    )
    return IntegrityError("INSERT INTO datasets ...", {}, orig)


class _WinnerLookup:
    """Fake for duplicates.find_existing recording its arguments."""

    def __init__(self, winner: ExistingDataset | None) -> None:
        self.winner = winner
        self.calls: list[tuple[str | None, str | None]] = []

    def __call__(
        self, normalized_target: str | None, normalized_final: str | None
    ) -> ExistingDataset | None:
        self.calls.append((normalized_target, normalized_final))
        return self.winner


def test_duplicate_url_race_falls_back_to_refreshing_the_winner() -> None:
    """A concurrent Add of the same new URL refreshes the winner (Property 6).

    The losing INSERT raises IntegrityError on ``uq_datasets_normalized_url``;
    the service must clean up its partial permanent objects (Req 4.5), look the
    winner up by normalized URL, refresh it with ``duplicate_submission``, and
    return ``refreshed`` carrying the *winner's* id — not a second dataset.
    """
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.existing_objects.add(keys.check_snapshot(_CHECK_ID, "u1"))  # snapshot copied too
    rec.insert_error = _integrity_error("uq_datasets_normalized_url")

    winner = ExistingDataset(id=_EXISTING_ID, name="Acme CRM", archived=False, status="updated")
    lookup = _WinnerLookup(winner)

    with _patched(store, rec), patch.object(service.duplicates, "find_existing", lookup):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    # Routed to the winner's refresh, not a new dataset.
    assert results[0].outcome == "refreshed"
    assert results[0].dataset_id == _EXISTING_ID
    assert len(rec.refreshes) == 1
    winner_id, trigger, capture = rec.refreshes[0]
    assert winner_id == _EXISTING_ID
    assert trigger == "duplicate_submission"
    assert capture.page_key == keys.check_page(_CHECK_ID, "u1")

    # Winner looked up by the item's normalized target/final URL.
    assert lookup.calls == [(item.normalized, service._normalized_final(item))]

    # Req 4.5: every partial permanent object copied under the losing id is
    # deleted; no orphaned permanent objects remain and nothing was enqueued.
    copied_dsts = {dst for _, dst in rec.copies}
    assert set(rec.deletes) == copied_dsts
    assert len(rec.deletes) == 3  # page, plan, snapshot
    assert rec.enqueues == []


@pytest.mark.parametrize(
    "outcome",
    ["refreshed", "restored_and_refreshed", "already_refreshing"],
)
def test_duplicate_url_race_maps_refresh_outcome(outcome: str) -> None:
    """The race fallback returns whatever the Refresh Service decides."""
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.insert_error = _integrity_error("uq_datasets_normalized_url")
    rec.refresh_outcome = outcome
    winner = ExistingDataset(id=_EXISTING_ID, name="Acme CRM", archived=False, status="updated")

    with (
        _patched(store, rec),
        patch.object(service.duplicates, "find_existing", _WinnerLookup(winner)),
    ):
        results = service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert results[0].outcome == outcome
    assert results[0].dataset_id == _EXISTING_ID


def test_unrelated_integrity_error_is_not_swallowed() -> None:
    """An IntegrityError on a *different* constraint propagates (not a dup race).

    Only ``uq_datasets_normalized_url`` triggers the refresh fallback. A NOT
    NULL / foreign-key / other-unique violation must surface so a real bug is
    not silently turned into a refresh. Partial objects are still cleaned up.
    """
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.insert_error = _integrity_error("datasets_pkey")  # unrelated violation
    lookup = _WinnerLookup(None)

    with (
        _patched(store, rec),
        patch.object(service.duplicates, "find_existing", lookup),
    ):
        with pytest.raises(IntegrityError):
            service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    # Did not try to find or refresh a winner; cleaned up; enqueued nothing.
    assert lookup.calls == []
    assert rec.refreshes == []
    assert set(rec.deletes) == {dst for _, dst in rec.copies}
    assert rec.enqueues == []


def test_duplicate_url_race_without_winner_raises() -> None:
    """If the race fired but no winner is found, surface the condition.

    The unique index just rejected our insert for a winner, so one should exist.
    If it has vanished (deleted between the violation and the lookup) we raise
    rather than reporting a false success.
    """
    item = _item(verdict_label="will_work")
    store = _FakeSessionStore(_session([item]))
    rec = _Recorder()
    rec.insert_error = _integrity_error("uq_datasets_normalized_url")

    with (
        _patched(store, rec),
        patch.object(service.duplicates, "find_existing", _WinnerLookup(None)),
    ):
        with pytest.raises(RuntimeError, match="no winning dataset"):
            service.add_items(_CHECK_ID, [AddRequestItem("u1")])

    assert rec.refreshes == []
    assert set(rec.deletes) == {dst for _, dst in rec.copies}
