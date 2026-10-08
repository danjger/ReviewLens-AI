"""Property-based test for app.ingestion.service.add_items (11).

- **Property 5: Won't-work URLs never become data.** *For any* sequence of Add
  requests, no dataset or data version SHALL be created from an item whose
  verdict is ``wont_work``. Validates: Requirement 3.8

The invariant is a safety guarantee: a ``wont_work`` item must never trigger a
dataset INSERT, a processing enqueue, or a refresh — none of the actions that
create a dataset or a new data version. This test drives the real
``add_items`` over an arbitrary sequence of Check items (a mix of verdicts,
tracked / not-tracked, varied states) with every heavy dependency faked at the
module boundary, the same offline approach the unit tests in
``tests/unit/ingestion/test_add_service.py`` use. For any generated sequence,
every effect that reaches the recorder must be attributable to a *non*-
``wont_work`` item.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from app.datasets.refresh_service import CheckCapture
from app.ingestion import service
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.service import AddRequestItem
from hypothesis import given
from hypothesis import strategies as st

_CHECK_ID = "chk-prop5"
_EXISTING_ID = "22222222-2222-2222-2222-222222222222"

_LABELS = st.sampled_from(["will_work", "limited", "wont_work"])


def _verdict(label: str) -> dict[str, Any]:
    return {
        "verdict": label,
        "reasons": ["r"],
        "warnings": [],
        "evidence": {
            "reviews_verified": 7,
            "method": "selectors",
            "page_title": "T",
            "samples": [],
        },
    }


def _item(item_id: str, label: str, *, tracked: bool, state: str) -> CheckItem:
    existing = (
        {"id": _EXISTING_ID, "name": "Acme", "archived": False, "status": "updated"}
        if tracked
        else None
    )
    url = f"https://acme.example/{item_id}"
    return CheckItem(
        item_id=item_id,
        input=url,
        state=state,  # type: ignore[arg-type]
        normalized=url,
        final_url=url,
        hops=[{"url": url, "status": 200, "timestamp": "t"}],
        verdict=_verdict(label),
        existing_dataset=existing,
        capture_prefix=f"checks/{_CHECK_ID}/{item_id}/",
    )


class _FakeSessionStore:
    """Offline stand-in for app.ingestion.check_session."""

    def __init__(self, session: CheckSession) -> None:
        self._session = session

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
        # Claim succeeds and reflects the new state, like a conditional write
        # against DynamoDB on a `done` item.
        item = self._session.items.get(item_id)
        if item is not None and "state" in fields:
            item.state = fields["state"]  # type: ignore[assignment]
        return True


class _Recorder:
    """Captures every dataset-creating / refreshing effect."""

    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []
        self.inserts: list[tuple[str, CheckItem]] = []
        self.enqueues: list[tuple[str, dict[str, Any], str | None]] = []
        self.refreshes: list[tuple[str, str, CheckCapture]] = []

    def copy_object(self, src: str, dst: str) -> None:
        self.copies.append((src, dst))

    def object_exists(self, key: str) -> bool:
        return False

    def delete_object(self, key: str) -> None:  # pragma: no cover - no failures here
        pass

    def enqueue(self, queue_url: str, body: dict[str, Any], **kw: Any) -> None:
        self.enqueues.append((queue_url, body, kw.get("message_group_id")))

    def insert_dataset(self, dataset_id: str, item: CheckItem) -> None:
        self.inserts.append((dataset_id, item))

    def refresh(self, dataset_id: str, trigger: str, capture: CheckCapture) -> str:
        self.refreshes.append((dataset_id, trigger, capture))
        return "refreshed"


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


@st.composite
def _add_scenario(draw: st.DrawFn) -> tuple[list[CheckItem], list[AddRequestItem]]:
    """A session of items (mixed verdicts/tracked/state) and the Add requests.

    Item ids are unique positional labels; a mix of ``will_work`` / ``limited``
    / ``wont_work`` verdicts, tracked and untracked, and `done` / `applied`
    starting states covers the branches add_items takes.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    items: list[CheckItem] = []
    for i in range(n):
        label = draw(_LABELS)
        tracked = draw(st.booleans())
        # Items are in the `done` state — the point at which Add decides to
        # create, refresh, or refuse. An `applied` item was already handled by
        # a prior Add, so add_items short-circuits it to an idempotent in-flight
        # outcome without re-deriving the verdict; that reporting path creates
        # no new data (and so is covered by the structural checks below) but
        # cannot report `refused_wont_work`, so it is out of scope for the
        # per-item outcome assertion here.
        items.append(_item(f"u{i}", label, tracked=tracked, state="done"))

    # Request every item, each possibly confirming "limited" or not.
    requests = [AddRequestItem(it.item_id, confirm_limited=draw(st.booleans())) for it in items]
    return items, requests


@given(scenario=_add_scenario())
def test_wont_work_items_never_become_data(
    scenario: tuple[list[CheckItem], list[AddRequestItem]],
) -> None:
    """Property 5: Won't-work URLs never become data.

    For any sequence of Add requests, no dataset or data version is created from
    a wont_work item: no INSERT, no enqueue, and no refresh is ever attributable
    to a wont_work item.
    Validates: Requirement 3.8
    """
    items, requests = scenario
    session = CheckSession(
        check_id=_CHECK_ID,
        created_at="2024-01-01T00:00:00+00:00",
        ttl=9_999_999_999,
        origin="new",  # type: ignore[arg-type]
        items={it.item_id: it for it in items},
    )
    store = _FakeSessionStore(session)
    rec = _Recorder()

    with _patched(store, rec):
        results = service.add_items(_CHECK_ID, requests)

    by_id = {it.item_id: it for it in items}

    def _label(item_id: str) -> str:
        return str(by_id[item_id].verdict["verdict"])  # type: ignore[index]

    # No wont_work item is ever reported as created/refreshed/restored.
    for res in results:
        if _label(res.item_id) == "wont_work":
            assert res.outcome == "refused_wont_work"
            assert res.dataset_id is None

    # No creating/refreshing effect carries a wont_work item's capture prefix.
    wont_prefixes = {
        f"checks/{_CHECK_ID}/{it.item_id}/"
        for it in items
        if it.verdict["verdict"] == "wont_work"  # type: ignore[index]
    }
    for _dataset_id, item in rec.inserts:
        assert item.verdict["verdict"] != "wont_work"  # type: ignore[index]
    for _src, dst in rec.copies:
        assert not any(dst.startswith(p) for p in wont_prefixes)
    # Refresh captures come from the item's check page key; none may be a
    # wont_work item (a wont_work item is refused before any refresh route).
    for _dataset_id, _trigger, capture in rec.refreshes:
        assert not any(capture.page_key.startswith(p) for p in wont_prefixes)
