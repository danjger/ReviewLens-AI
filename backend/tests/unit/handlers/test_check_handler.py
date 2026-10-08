"""Unit tests for app.handlers.check.CheckHandler (dataset-ingestion task 4.2).

These exercise the handler's *orchestration* with every heavy dependency
(probe, capture, viability, duplicate lookup, DynamoDB, S3, EventBridge) faked
at the module boundary, so they are fast and fully offline. They cover:

- a dropped duplicate/concurrent delivery (lost claim) does no work;
- the happy path: claim → probe → capture → assess → duplicate lookup → store
  the plan, write the verdict + final URL + hops + existing match, publish
  ``check.updated`` (Requirements 2.4, 3.1);
- a client-side redirect records a ``client_redirect`` hop and uses the
  browser's final URL (Requirement 2.8);
- a non-200 probe → ``wont_work`` with the status, no capture (Requirement 2.2);
- an SSRF block → ``wont_work`` "Address not allowed";
- a non-200 browser main status → ``wont_work`` with the status (Requirement
  2.7);
- a per-item timeout → ``wont_work`` "Page took too long to load" (Req 3.10);
- a crash on the final SQS attempt marks the item ``error`` with Retry, and
  re-raises so SQS redrives; an earlier attempt re-raises without flipping the
  state;
- refresh-origin handling (task 6.2, Requirement 6.9): a ``will_work`` verdict
  auto-refreshes and marks the item ``applied``; a ``limited`` verdict for a
  dataset previously ``limited`` auto-refreshes too; a ``limited`` verdict for a
  dataset previously ``will_work`` leaves the item ``awaiting_confirmation`` and
  does not refresh; a ``wont_work`` verdict creates no version row (no refresh)
  and appends a failed-refresh event through ``db.status.log_event``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.capture.engine import CaptureBlockedError, CaptureResult
from app.consumer import MAX_RECEIVE_COUNT, MessageMeta
from app.extraction.models import ExtractionPlan, NextPageRule
from app.handlers import check as check_mod
from app.handlers.check import CheckHandler
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.duplicates import ExistingDataset
from app.ingestion.robots import RobotsResult
from app.ingestion.url_validator import Hop, ProbeResult, SsrfError
from app.ingestion.verdict import Verdict

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    """A tiny stand-in for app.ingestion.check_session used by the handler.

    Tracks the claim outcome, the stored session, and records every
    ``set_item_fields`` call so tests can assert what the handler wrote.
    """

    def __init__(self, session: CheckSession, *, claim: bool = True) -> None:
        self._session = session
        self._claim = claim
        self.claims: list[tuple[str, str]] = []
        self.writes: list[dict[str, Any]] = []

    def claim_item(self, check_id: str, item_id: str) -> bool:
        self.claims.append((check_id, item_id))
        return self._claim

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
        self.writes.append(
            {
                "check_id": check_id,
                "item_id": item_id,
                "fields": fields,
                "expected_state": expected_state,
            }
        )
        return True


def _session(origin: str = "new", refresh_dataset_id: str | None = None) -> CheckSession:
    item = CheckItem(
        item_id="u1",
        input="https://a.example/reviews",
        state="checking",
        normalized="https://a.example/reviews",
        capture_prefix="checks/chk-1/u1/",
    )
    return CheckSession(
        check_id="chk-1",
        created_at="2024-01-01T00:00:00+00:00",
        ttl=0,
        origin=origin,  # type: ignore[arg-type]
        items={"u1": item},
        refresh_dataset_id=refresh_dataset_id,
    )


def _plan() -> ExtractionPlan:
    return ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="selector", css=".next"),
    )


def _capture(
    *, final_url: str, main_status: int | None = 200, redirected: bool = False
) -> CaptureResult:
    return CaptureResult(
        final_url=final_url,
        main_status=main_status,
        page_title="Acme CRM Reviews",
        html_key="checks/chk-1/u1/page.html",
        snapshot_key="checks/chk-1/u1/snapshot.png",
        redirected=redirected,
    )


def _meta(receive_count: int = 1) -> MessageMeta:
    return MessageMeta(message_id="m-1", receive_count=receive_count)


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake every heavy dependency; return the captured events/puts for asserts."""
    events: list[dict[str, Any]] = []
    puts: list[tuple[str, bytes]] = []

    monkeypatch.setattr(
        check_mod, "publish_event", lambda detail_type, detail: events.append(detail)
    )
    monkeypatch.setattr(check_mod.s3, "get_text", lambda key: "<html><body>reviews</body></html>")
    monkeypatch.setattr(
        check_mod.s3, "put_bytes", lambda key, body, content_type: puts.append((key, body))
    )
    monkeypatch.setattr(check_mod, "robots_check", lambda url: RobotsResult(allowed=True))
    monkeypatch.setattr(check_mod.duplicates, "find_existing", lambda a, b: None)
    return {"events": events, "puts": puts}


# ---------------------------------------------------------------------------
# Claim / drop
# ---------------------------------------------------------------------------


def test_lost_claim_drops_the_message(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session(), claim=False)
    monkeypatch.setattr(check_mod, "check_session", store)
    # Probe must never run when the claim is lost.
    monkeypatch.setattr(
        check_mod, "probe", lambda *a, **k: pytest.fail("probe ran despite a lost claim")
    )

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    assert store.claims == [("chk-1", "u1")]
    assert store.writes == []
    assert wiring["events"] == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_writes_verdict_plan_and_event(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(
            final_url=url,
            status=200,
            ok=True,
            hops=[Hop(url=url, status=200, timestamp="t0")],
        ),
    )
    monkeypatch.setattr(
        check_mod.capture_engine,
        "render",
        lambda url, prefix: _capture(final_url=url),
    )
    verdict = Verdict(verdict="will_work", reasons=["24 reviews verified"])
    monkeypatch.setattr(check_mod, "assess", lambda view, final_url, robots: (verdict, _plan()))

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    # Plan stored at the check plan key.
    assert any(key == "checks/chk-1/u1/plan.json" for key, _ in wiring["puts"])
    # One write to the item, moving it to done with the verdict + final URL.
    assert len(store.writes) == 1
    write = store.writes[0]
    assert write["expected_state"] == "checking"
    assert write["fields"]["state"] == "done"
    assert write["fields"]["verdict"]["verdict"] == "will_work"
    assert write["fields"]["final_url"] == "https://a.example/reviews"
    assert write["fields"]["hops"][0]["status"] == 200
    # check.updated published with the verdict label.
    assert wiring["events"] == [
        {"check_id": "chk-1", "item_id": "u1", "state": "done", "verdict": "will_work"}
    ]


def test_existing_dataset_is_recorded(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(final_url=url, status=200, ok=True, hops=[]),
    )
    monkeypatch.setattr(
        check_mod.capture_engine, "render", lambda url, prefix: _capture(final_url=url)
    )
    monkeypatch.setattr(
        check_mod,
        "assess",
        lambda *a, **k: (Verdict(verdict="will_work"), _plan()),
    )
    monkeypatch.setattr(
        check_mod.duplicates,
        "find_existing",
        lambda a, b: ExistingDataset(id="ds-9", name="Acme CRM", archived=False, status="updated"),
    )

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    existing = store.writes[0]["fields"]["existing_dataset"]
    assert existing == {"id": "ds-9", "name": "Acme CRM", "archived": False, "status": "updated"}


# ---------------------------------------------------------------------------
# Client-side redirect
# ---------------------------------------------------------------------------


def test_client_redirect_records_hop_and_uses_browser_final_url(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(final_url=url, status=200, ok=True, hops=[]),
    )
    monkeypatch.setattr(
        check_mod.capture_engine,
        "render",
        lambda url, prefix: _capture(
            final_url="https://a.example/reviews/landing", redirected=True
        ),
    )
    monkeypatch.setattr(check_mod, "assess", lambda *a, **k: (Verdict(verdict="limited"), _plan()))

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    fields = store.writes[0]["fields"]
    assert fields["final_url"] == "https://a.example/reviews/landing"
    client_hop = [h for h in fields["hops"] if h.get("kind") == "client_redirect"]
    assert len(client_hop) == 1
    assert client_hop[0]["url"] == "https://a.example/reviews/landing"


# ---------------------------------------------------------------------------
# Pre-capture failures → wont_work, no capture
# ---------------------------------------------------------------------------


def test_non_200_probe_is_wont_work_without_capture(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(final_url=url, status=404, ok=False, hops=[], reason="Status 404"),
    )
    monkeypatch.setattr(
        check_mod.capture_engine,
        "render",
        lambda *a, **k: pytest.fail("capture ran despite a failed probe"),
    )

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    fields = store.writes[0]["fields"]
    assert fields["verdict"]["verdict"] == "wont_work"
    assert fields["verdict"]["reasons"] == ["Status 404"]
    assert wiring["puts"] == []  # no plan stored


def test_ssrf_block_is_wont_work(monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)

    def _probe(url: str) -> ProbeResult:
        raise SsrfError("Address not allowed")

    monkeypatch.setattr(check_mod, "probe", _probe)

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    assert store.writes[0]["fields"]["verdict"]["verdict"] == "wont_work"
    assert store.writes[0]["fields"]["verdict"]["reasons"] == ["Address not allowed"]


def test_capture_blocked_is_wont_work(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod, "probe", lambda url: ProbeResult(final_url=url, status=200, ok=True, hops=[])
    )

    def _render(url: str, prefix: str) -> CaptureResult:
        raise CaptureBlockedError("Address not allowed")

    monkeypatch.setattr(check_mod.capture_engine, "render", _render)

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    assert store.writes[0]["fields"]["verdict"]["verdict"] == "wont_work"
    assert store.writes[0]["fields"]["verdict"]["reasons"] == ["Address not allowed"]


def test_non_200_browser_main_status_is_wont_work(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)
    monkeypatch.setattr(
        check_mod, "probe", lambda url: ProbeResult(final_url=url, status=200, ok=True, hops=[])
    )
    monkeypatch.setattr(
        check_mod.capture_engine,
        "render",
        lambda url, prefix: _capture(final_url=url, main_status=403),
    )
    monkeypatch.setattr(
        check_mod, "assess", lambda *a, **k: pytest.fail("assess ran on a 403 page")
    )

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    assert store.writes[0]["fields"]["verdict"]["verdict"] == "wont_work"
    assert store.writes[0]["fields"]["verdict"]["reasons"] == ["Status 403"]


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_is_wont_work_page_too_slow(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("CHECK_TIMEOUT_S", "1")
    get_settings.cache_clear()

    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)

    def _slow_probe(url: str) -> ProbeResult:
        time.sleep(3)
        return ProbeResult(final_url=url, status=200, ok=True, hops=[])

    monkeypatch.setattr(check_mod, "probe", _slow_probe)

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    fields = store.writes[0]["fields"]
    assert fields["verdict"]["verdict"] == "wont_work"
    assert fields["verdict"]["reasons"] == ["Page took too long to load"]
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Crash handling (SQS retry + error on final attempt)
# ---------------------------------------------------------------------------


def test_crash_on_final_attempt_marks_error_and_reraises(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)

    def _boom(url: str) -> ProbeResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(check_mod, "probe", _boom)

    with pytest.raises(RuntimeError):
        CheckHandler().handle(
            {"check_id": "chk-1", "item_id": "u1"}, _meta(receive_count=MAX_RECEIVE_COUNT)
        )

    # The item was flipped to error (so the UI shows Retry) and an event sent.
    assert store.writes[-1]["fields"] == {"state": "error"}
    assert store.writes[-1]["expected_state"] == "checking"
    assert wiring["events"][-1] == {"check_id": "chk-1", "item_id": "u1", "state": "error"}


def test_crash_before_final_attempt_reraises_without_marking_error(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    store = _FakeSessionStore(_session())
    monkeypatch.setattr(check_mod, "check_session", store)

    def _boom(url: str) -> ProbeResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(check_mod, "probe", _boom)

    with pytest.raises(RuntimeError):
        CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta(receive_count=1))

    # Not the final attempt: no error write, so a retry can re-claim it.
    assert store.writes == []
    assert wiring["events"] == []


# ---------------------------------------------------------------------------
# Refresh origin (task 6.2, Requirement 6.9)
# ---------------------------------------------------------------------------


def _wire_refresh_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    verdict: Verdict,
) -> list[tuple[str, str, Any]]:
    """Wire probe/capture/assess so a refresh item reaches *verdict*.

    Returns the list that records Refresh Service calls as
    ``(dataset_id, trigger, capture)`` so a test can assert whether (and how) a
    refresh was started.
    """
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(final_url=url, status=200, ok=True, hops=[]),
    )
    monkeypatch.setattr(
        check_mod.capture_engine, "render", lambda url, prefix: _capture(final_url=url)
    )
    monkeypatch.setattr(check_mod, "assess", lambda *a, **k: (verdict, _plan()))

    refreshes: list[tuple[str, str, Any]] = []

    def _refresh(dataset_id: str, trigger: str, capture: Any) -> str:
        refreshes.append((dataset_id, trigger, capture))
        return "refreshed"

    monkeypatch.setattr(check_mod.refresh_service, "refresh", _refresh)
    return refreshes


def test_refresh_origin_will_work_auto_refreshes_and_marks_applied(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """Requirement 6.9: a will_work refresh starts automatically, item applied."""
    store = _FakeSessionStore(_session(origin="refresh", refresh_dataset_id="ds-7"))
    monkeypatch.setattr(check_mod, "check_session", store)
    refreshes = _wire_refresh_pipeline(monkeypatch, Verdict(verdict="will_work"))
    # Previous verdict is irrelevant for will_work; it must not even be needed.
    monkeypatch.setattr(check_mod, "_previous_verdict", lambda ds: "limited")

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    # Refresh started with the right dataset, trigger, and a CheckCapture from
    # this item's S3 keys (storage.keys).
    assert len(refreshes) == 1
    dataset_id, trigger, capture = refreshes[0]
    assert dataset_id == "ds-7"
    assert trigger == "manual_refresh"
    assert isinstance(capture, check_mod.CheckCapture)
    assert capture.page_key == "checks/chk-1/u1/page.html"
    assert capture.plan_key == "checks/chk-1/u1/plan.json"
    assert capture.snapshot_key == "checks/chk-1/u1/snapshot.png"
    # Item finished `applied`, plan stored, event carries the state + verdict.
    assert store.writes[-1]["fields"]["state"] == "applied"
    assert any(key == "checks/chk-1/u1/plan.json" for key, _ in wiring["puts"])
    assert wiring["events"][-1] == {
        "check_id": "chk-1",
        "item_id": "u1",
        "state": "applied",
        "verdict": "will_work",
    }


def test_refresh_origin_limited_after_limited_auto_refreshes(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """Requirement 6.9: limited for a previously-limited dataset auto-refreshes."""
    store = _FakeSessionStore(_session(origin="refresh", refresh_dataset_id="ds-7"))
    monkeypatch.setattr(check_mod, "check_session", store)
    refreshes = _wire_refresh_pipeline(monkeypatch, Verdict(verdict="limited"))
    monkeypatch.setattr(check_mod, "_previous_verdict", lambda ds: "limited")

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    assert len(refreshes) == 1
    assert refreshes[0][0] == "ds-7"
    assert store.writes[-1]["fields"]["state"] == "applied"
    assert wiring["events"][-1]["state"] == "applied"


def test_refresh_origin_limited_after_will_work_awaits_confirmation(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """Requirement 6.9: limited after a will_work waits for the analyst."""
    store = _FakeSessionStore(_session(origin="refresh", refresh_dataset_id="ds-7"))
    monkeypatch.setattr(check_mod, "check_session", store)
    refreshes = _wire_refresh_pipeline(monkeypatch, Verdict(verdict="limited"))
    monkeypatch.setattr(check_mod, "_previous_verdict", lambda ds: "will_work")

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    # No refresh started; item parked for confirmation.
    assert refreshes == []
    assert store.writes[-1]["fields"]["state"] == "awaiting_confirmation"
    assert wiring["events"][-1] == {
        "check_id": "chk-1",
        "item_id": "u1",
        "state": "awaiting_confirmation",
        "verdict": "limited",
    }


def test_refresh_origin_wont_work_logs_failure_and_creates_no_version(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """Requirement 6.9: wont_work keeps existing data, logs a failed-refresh event."""
    store = _FakeSessionStore(_session(origin="refresh", refresh_dataset_id="ds-7"))
    monkeypatch.setattr(check_mod, "check_session", store)
    refreshes = _wire_refresh_pipeline(
        monkeypatch,
        Verdict(verdict="wont_work", reasons=["The page is a bot or CAPTCHA challenge"]),
    )
    # _previous_verdict must not be consulted on the wont_work path.
    monkeypatch.setattr(
        check_mod,
        "_previous_verdict",
        lambda ds: pytest.fail("previous verdict read on a wont_work refresh"),
    )

    events: list[tuple[str, str, dict[str, Any] | None]] = []

    def _log_event(dataset_id: str, message: str, extra: dict[str, Any] | None = None) -> None:
        events.append((dataset_id, message, extra))

    monkeypatch.setattr(check_mod, "log_event", _log_event)

    CheckHandler().handle({"check_id": "chk-1", "item_id": "u1"}, _meta())

    # No refresh (so no new dataset_versions row via the Refresh Service).
    assert refreshes == []
    # A failed-refresh event was appended to the dataset's status log.
    assert len(events) == 1
    dataset_id, message, extra = events[0]
    assert dataset_id == "ds-7"
    assert "Refresh failed" in message
    assert extra == {
        "trigger": "manual_refresh",
        "verdict": "wont_work",
        "reasons": ["The page is a bot or CAPTCHA challenge"],
    }
    # Item finished `done` with the wont_work verdict and a check.updated event.
    assert store.writes[-1]["fields"]["state"] == "done"
    assert store.writes[-1]["fields"]["verdict"]["verdict"] == "wont_work"
    assert wiring["events"][-1] == {
        "check_id": "chk-1",
        "item_id": "u1",
        "state": "done",
        "verdict": "wont_work",
    }


def test_refresh_origin_crash_on_final_attempt_marks_error(
    monkeypatch: pytest.MonkeyPatch, wiring: dict[str, Any]
) -> None:
    """A crashing refresh check is marked error on the final attempt, like `new`."""
    store = _FakeSessionStore(_session(origin="refresh", refresh_dataset_id="ds-7"))
    monkeypatch.setattr(check_mod, "check_session", store)

    def _boom(url: str) -> ProbeResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(check_mod, "probe", _boom)

    with pytest.raises(RuntimeError):
        CheckHandler().handle(
            {"check_id": "chk-1", "item_id": "u1"}, _meta(receive_count=MAX_RECEIVE_COUNT)
        )

    assert store.writes[-1]["fields"] == {"state": "error"}
    assert wiring["events"][-1] == {"check_id": "chk-1", "item_id": "u1", "state": "error"}
