"""Unit tests for app.datasets.refresh_api (dataset-library task 3).

These cover the refresh API's domain *decisions* offline, with the database
(``_load_target`` / ``_set_refresh_check_id``), the Check Session store, the
queue, the Refresh Service, and the upload parser faked at the module boundary —
the same approach the check-handler unit tests use. The behaviour against the
real backing services (a ``will_work`` refresh creating a new version, a 404
page keeping the data, 409 while processing, the confirm flow, and manual vs
duplicate producing the same record) is the integration suite
(``tests/integration/datasets/test_refresh_int.py``).

What they prove:

- :func:`start_refresh` creates a one-item refresh-origin Check Session carrying
  the dataset id, records ``refresh_check_id`` *before* enqueueing, enqueues the
  single item with IDs only onto ``check_queue_url``, and returns the Check id
  (Requirements 4.1, 4.2).
- the 409 ``ALREADY_REFRESHING`` guard fires while the dataset is ``requested``/
  ``processing`` or an outstanding refresh Check is recorded, and does **not**
  fire for a stale recorded id whose Check has resolved/expired (Requirement
  4.4).
- an upload dataset is refused by ``start_refresh`` and a URL dataset is refused
  by :func:`refresh_from_upload`.
- :func:`confirm_refresh` proceeds only for an ``awaiting_confirmation`` item of
  the matching Check and calls the Refresh Service with
  ``trigger="manual_refresh"`` and a CheckCapture from the Check's S3 keys
  (Requirement 4.3).
- :func:`refresh_from_upload` re-validates the staged file, stages mapping.json,
  and calls the Refresh Service with ``trigger="upload_replace"`` and an
  UploadCapture from the upload's S3 keys (Requirement 4.5).
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import AppValidationError, ConflictError, NotFoundError
from app.datasets import refresh_api
from app.datasets.refresh_api import _RefreshTarget
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.upload_parser import UploadInvalidError, UploadPreview

_DS = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    """Records put_session / set_item_fields calls and serves get_session."""

    def __init__(self, session: CheckSession | None = None) -> None:
        self.session = session
        self.put_calls: list[dict[str, Any]] = []
        self.field_writes: list[dict[str, Any]] = []

    def put_session(
        self,
        check_id: str,
        origin: str,
        items: list[CheckItem],
        refresh_dataset_id: str | None = None,
    ) -> CheckSession:
        self.put_calls.append(
            {
                "check_id": check_id,
                "origin": origin,
                "items": items,
                "refresh_dataset_id": refresh_dataset_id,
            }
        )
        self.session = CheckSession(
            check_id=check_id,
            created_at="2024-01-01T00:00:00+00:00",
            ttl=0,
            origin=origin,  # type: ignore[arg-type]
            items={it.item_id: it for it in items},
            refresh_dataset_id=refresh_dataset_id,
        )
        return self.session

    def get_session(self, check_id: str) -> CheckSession | None:
        if self.session is not None and self.session.check_id == check_id:
            return self.session
        return None

    def set_item_fields(
        self,
        check_id: str,
        item_id: str,
        fields: dict[str, Any],
        *,
        expected_state: str | None = None,
    ) -> bool:
        self.field_writes.append(
            {"check_id": check_id, "item_id": item_id, "fields": fields, "expected": expected_state}
        )
        return True


def _target(
    *,
    source_type: str = "url",
    original_url: str | None = "https://g2.com/acme/reviews",
    status: str = "updated",
    refresh_check_id: str | None = None,
) -> _RefreshTarget:
    return _RefreshTarget(
        id=_DS,
        source_type=source_type,
        original_url=original_url,
        status=status,
        refresh_check_id=refresh_check_id,
    )


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the DB target load, the id write, the queue, and the Refresh Service."""
    recorded: dict[str, Any] = {
        "enqueued": [],
        "set_check_ids": [],
        "cleared": [],
        "refreshes": [],
        "put_bytes": [],
    }

    def _enqueue(queue_url: str, body: dict[str, Any], **_: Any) -> None:
        recorded["enqueued"].append((queue_url, body))

    def _set_id(dataset_id: str, check_id: str) -> None:
        recorded["set_check_ids"].append((dataset_id, check_id))

    def _clear(dataset_id: str) -> None:
        recorded["cleared"].append(dataset_id)

    def _refresh(dataset_id: str, trigger: str, capture: Any) -> str:
        recorded["refreshes"].append((dataset_id, trigger, capture))
        return "refreshed"

    def _put_bytes(key: str, body: bytes, *, content_type: str) -> None:
        recorded["put_bytes"].append((key, body, content_type))

    monkeypatch.setattr(refresh_api, "enqueue", _enqueue)
    monkeypatch.setattr(refresh_api, "_set_refresh_check_id", _set_id)
    monkeypatch.setattr(refresh_api, "clear_refresh_check_id", _clear)
    monkeypatch.setattr(refresh_api.refresh_service, "refresh", _refresh)
    monkeypatch.setattr(refresh_api.s3, "put_bytes", _put_bytes)
    # A settings object exposing check_queue_url without touching the env.
    monkeypatch.setattr(
        refresh_api, "get_settings", lambda: type("S", (), {"check_queue_url": "q-check"})()
    )
    return recorded


# ---------------------------------------------------------------------------
# start_refresh (Requirements 4.1, 4.2, 4.4)
# ---------------------------------------------------------------------------


def test_start_refresh_creates_session_records_id_and_enqueues(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """A URL dataset refresh: refresh-origin session + recorded id + enqueue."""
    store = _FakeSessionStore()
    monkeypatch.setattr(refresh_api, "check_session", store)
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())

    check_id = refresh_api.start_refresh(_DS)

    # A refresh-origin, one-item session carrying the dataset id was created.
    assert len(store.put_calls) == 1
    call = store.put_calls[0]
    assert call["origin"] == "refresh"
    assert call["refresh_dataset_id"] == _DS
    assert [it.item_id for it in call["items"]] == [refresh_api.REFRESH_ITEM_ID]
    assert call["items"][0].input == "https://g2.com/acme/reviews"
    assert call["items"][0].state == "pending"

    # The running check id was recorded, then the single item enqueued (IDs only).
    assert wiring["set_check_ids"] == [(_DS, check_id)]
    assert wiring["enqueued"] == [
        ("q-check", {"check_id": check_id, "item_id": refresh_api.REFRESH_ITEM_ID})
    ]


def test_start_refresh_records_id_before_enqueue(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """The id is recorded before the enqueue so the row shows 'Checking page…'."""
    store = _FakeSessionStore()
    order: list[str] = []
    monkeypatch.setattr(refresh_api, "check_session", store)
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())
    monkeypatch.setattr(refresh_api, "_set_refresh_check_id", lambda ds, cid: order.append("set"))
    monkeypatch.setattr(refresh_api, "enqueue", lambda *a, **k: order.append("enqueue"))

    refresh_api.start_refresh(_DS)

    assert order == ["set", "enqueue"]


def test_start_refresh_upload_dataset_is_rejected(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore())
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(source_type="upload"))
    with pytest.raises(AppValidationError):
        refresh_api.start_refresh(_DS)


def test_start_refresh_without_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore())
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(original_url=None))
    with pytest.raises(AppValidationError):
        refresh_api.start_refresh(_DS)


@pytest.mark.parametrize("status", ["requested", "processing"])
def test_start_refresh_409_while_in_flight(
    status: str, monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """A requested/processing dataset refuses a new refresh (Requirement 4.4)."""
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore())
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(status=status))
    with pytest.raises(ConflictError) as exc:
        refresh_api.start_refresh(_DS)
    assert exc.value.code == refresh_api.ALREADY_REFRESHING_CODE
    assert exc.value.status_code == 409


def test_start_refresh_409_while_check_outstanding(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """A recorded refresh Check that is still running blocks a new refresh."""
    running = CheckSession(
        check_id="chk-run",
        created_at="x",
        ttl=10**12,
        origin="refresh",
        items={
            refresh_api.REFRESH_ITEM_ID: CheckItem(
                item_id=refresh_api.REFRESH_ITEM_ID, input="u", state="checking"
            )
        },
        refresh_dataset_id=_DS,
    )
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore(running))
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(refresh_check_id="chk-run"))
    with pytest.raises(ConflictError):
        refresh_api.start_refresh(_DS)


def test_start_refresh_stale_recorded_id_does_not_block(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """A recorded id whose Check has resolved/expired does not wedge the dataset."""
    store = _FakeSessionStore(session=None)  # get_session returns None (expired)
    monkeypatch.setattr(refresh_api, "check_session", store)
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(refresh_check_id="chk-old"))
    # Should NOT raise; it proceeds to create a fresh session.
    check_id = refresh_api.start_refresh(_DS)
    assert check_id != "chk-old"
    assert wiring["enqueued"]


# ---------------------------------------------------------------------------
# confirm_refresh (Requirement 4.3)
# ---------------------------------------------------------------------------


def _awaiting_session(check_id: str = "chk-1") -> CheckSession:
    return CheckSession(
        check_id=check_id,
        created_at="x",
        ttl=10**12,
        origin="refresh",
        items={
            refresh_api.REFRESH_ITEM_ID: CheckItem(
                item_id=refresh_api.REFRESH_ITEM_ID,
                input="https://g2.com/acme/reviews",
                state="awaiting_confirmation",
            )
        },
        refresh_dataset_id=_DS,
    )


def test_confirm_refresh_proceeds_with_manual_refresh(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_awaiting_session())
    monkeypatch.setattr(refresh_api, "check_session", store)
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())

    outcome = refresh_api.confirm_refresh(_DS, "chk-1")

    assert outcome == "refreshed"
    assert len(wiring["refreshes"]) == 1
    dataset_id, trigger, capture = wiring["refreshes"][0]
    assert dataset_id == _DS
    assert trigger == "manual_refresh"
    assert capture.page_key == "checks/chk-1/u1/page.html"
    assert capture.plan_key == "checks/chk-1/u1/plan.json"
    assert capture.snapshot_key == "checks/chk-1/u1/snapshot.png"
    # The item was marked applied (guarded on awaiting_confirmation).
    assert store.field_writes[-1]["fields"] == {"state": "applied"}
    assert store.field_writes[-1]["expected"] == "awaiting_confirmation"


def test_confirm_refresh_unknown_check_is_404(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore(session=None))
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())
    with pytest.raises(NotFoundError):
        refresh_api.confirm_refresh(_DS, "missing")


def test_confirm_refresh_wrong_dataset_is_404(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    session = _awaiting_session()
    session.refresh_dataset_id = "someone-else"
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore(session))
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())
    with pytest.raises(NotFoundError):
        refresh_api.confirm_refresh(_DS, "chk-1")


def test_confirm_refresh_item_not_awaiting_is_404(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    session = _awaiting_session()
    session.items[refresh_api.REFRESH_ITEM_ID].state = "done"
    monkeypatch.setattr(refresh_api, "check_session", _FakeSessionStore(session))
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target())
    with pytest.raises(NotFoundError):
        refresh_api.confirm_refresh(_DS, "chk-1")


# ---------------------------------------------------------------------------
# refresh_from_upload (Requirement 4.5)
# ---------------------------------------------------------------------------


def _preview() -> UploadPreview:
    return UploadPreview(
        columns=["text"],
        suggested_mapping={"text": "text"},
        sample_rows=[{"text": "ok"}],
        usable_rows=3,
        will_keep=3,
        keep_rule="first_in_file",
    )


def test_refresh_from_upload_stages_mapping_and_calls_refresh(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(source_type="upload"))
    monkeypatch.setattr(refresh_api.upload_parser, "preview_upload", lambda uid: _preview())

    outcome = refresh_api.refresh_from_upload(_DS, "up-9", {"text": "Body"})

    assert outcome == "refreshed"
    # mapping.json was staged next to the upload (key from storage.keys).
    assert any(key == "uploads/up-9/mapping.json" for key, _, _ in wiring["put_bytes"])
    # Refresh Service called with upload_replace and an UploadCapture.
    dataset_id, trigger, capture = wiring["refreshes"][0]
    assert dataset_id == _DS
    assert trigger == "upload_replace"
    assert capture.file_key == "uploads/up-9/file"
    assert capture.mapping_key == "uploads/up-9/mapping.json"


def test_refresh_from_upload_rejects_url_dataset(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(source_type="url"))
    with pytest.raises(AppValidationError):
        refresh_api.refresh_from_upload(_DS, "up-9", {})


def test_refresh_from_upload_invalid_file_is_422(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    monkeypatch.setattr(refresh_api, "_load_target", lambda ds: _target(source_type="upload"))

    def _boom(uid: str) -> UploadPreview:
        raise UploadInvalidError("no usable text column")

    monkeypatch.setattr(refresh_api.upload_parser, "preview_upload", _boom)
    with pytest.raises(AppValidationError):
        refresh_api.refresh_from_upload(_DS, "up-9", {})
