"""Route tests for the upload preview + submit endpoints (task 8.3).

These exercise the two endpoints added in dataset-ingestion task 8.3 through a
FastAPI ``TestClient``, with the parser and the Add service faked at the
router's module boundary (same style as ``test_upload_api.py`` /
``test_add_api.py``). They stay fully offline and assert the HTTP contract from
the design's "API endpoints" and "Error Handling" tables and Requirements 7.2,
7.3, and 7.4:

``POST /uploads/{upload_id}/preview``:

- happy path → ``200`` with the preview shape ``{columns, suggested_mapping,
  sample_rows, usable_rows, will_keep, keep_rule}`` (Requirement 7.2/7.6);
- an invalid staged file → ``422`` carrying the parser's message, with the
  parser's own delete-on-invalid behaviour relied on (Requirement 7.3).

``POST /datasets/upload``:

- happy path → ``201`` with ``{id}`` and ``create_from_upload`` called with the
  trimmed name, mapping, and description (Requirement 7.4);
- an empty / whitespace-only / missing name → ``422`` and the service is never
  called (Requirement 7.4: the name is required);
- an invalid staged upload (``UploadInvalidError`` from the service) → ``422``
  with the parser's message (Requirement 7.3).
"""

from __future__ import annotations

from typing import Any

import pytest
from app.ingestion import api as api_mod
from app.ingestion.upload_parser import UploadInvalidError, UploadPreview
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Client wiring (uploads + datasets routers, shared error handlers)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    """A TestClient mounting the uploads + datasets routers under /api.

    Mirrors app.api wiring (routers under /api + the shared error handlers and
    the Retry-After header) without the origin-guard middleware, so tests focus
    on the upload preview/submit contract.
    """
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api_mod.register_rate_limit_header(app)
    app.include_router(api_mod.uploads_router, prefix="/api")
    app.include_router(api_mod.datasets_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


_PREVIEW = UploadPreview(
    columns=["review", "stars", "when"],
    suggested_mapping={"text": "review", "rating": "stars", "date": "when"},
    sample_rows=[{"review": "Great product", "stars": "5", "when": "2024-01-02"}],
    usable_rows=1500,
    will_keep=1000,
    keep_rule="most_recent_by_date",
)


# ---------------------------------------------------------------------------
# POST /uploads/{upload_id}/preview
# ---------------------------------------------------------------------------


def test_preview_returns_200_with_preview_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid staged file previews with the design's response shape."""
    calls: list[str] = []

    def _fake_preview(upload_id: str) -> UploadPreview:
        calls.append(upload_id)
        return _PREVIEW

    monkeypatch.setattr(api_mod.upload_parser, "preview_upload", _fake_preview)

    resp = client.post("/api/uploads/up-123/preview")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "columns",
        "suggested_mapping",
        "sample_rows",
        "usable_rows",
        "will_keep",
        "keep_rule",
    }
    assert body["columns"] == ["review", "stars", "when"]
    assert body["suggested_mapping"] == {"text": "review", "rating": "stars", "date": "when"}
    assert body["usable_rows"] == 1500
    assert body["will_keep"] == 1000
    assert body["keep_rule"] == "most_recent_by_date"
    assert calls == ["up-123"]


def test_preview_invalid_file_is_422_with_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid staged file → 422 carrying the parser's user-facing message.

    The parser deletes the staged object itself (Requirement 7.3); the route's
    job is only to translate the error into the shared 422 envelope.
    """

    def _raise(upload_id: str) -> UploadPreview:
        raise UploadInvalidError("No review text column was found.")

    monkeypatch.setattr(api_mod.upload_parser, "preview_upload", _raise)

    resp = client.post("/api/uploads/up-bad/preview")

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "No review text column was found."


# ---------------------------------------------------------------------------
# POST /datasets/upload
# ---------------------------------------------------------------------------


class _FakeService:
    """Records create_from_upload calls and replays a canned id or error."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dataset_id = "ds-upload-1"
        self.raise_invalid: str | None = None

    def create_from_upload(
        self,
        upload_id: str,
        name: str,
        mapping: dict[str, str],
        description: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "upload_id": upload_id,
                "name": name,
                "mapping": mapping,
                "description": description,
            }
        )
        if self.raise_invalid is not None:
            raise UploadInvalidError(self.raise_invalid)
        return self.dataset_id


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> _FakeService:
    svc = _FakeService()
    monkeypatch.setattr(api_mod, "service", svc)
    return svc


def test_submit_upload_returns_201_with_id(client: TestClient, fake_service: _FakeService) -> None:
    """A valid submit → 201 {id}; the service is called with the mapping/desc."""
    resp = client.post(
        "/api/datasets/upload",
        json={
            "upload_id": "up-1",
            "name": "Acme reviews export",
            "mapping": {"text": "review", "rating": "stars"},
            "description": "Exported from Acme admin",
        },
    )

    assert resp.status_code == 201
    assert resp.json() == {"id": "ds-upload-1"}
    assert fake_service.calls == [
        {
            "upload_id": "up-1",
            "name": "Acme reviews export",
            "mapping": {"text": "review", "rating": "stars"},
            "description": "Exported from Acme admin",
        }
    ]


def test_submit_upload_trims_name(client: TestClient, fake_service: _FakeService) -> None:
    """A name with surrounding whitespace is trimmed before the service call."""
    resp = client.post(
        "/api/datasets/upload",
        json={"upload_id": "up-1", "name": "  Padded name  ", "mapping": {"text": "review"}},
    )
    assert resp.status_code == 201
    assert fake_service.calls[0]["name"] == "Padded name"


def test_submit_upload_defaults_description_to_none(
    client: TestClient, fake_service: _FakeService
) -> None:
    resp = client.post(
        "/api/datasets/upload",
        json={"upload_id": "up-1", "name": "No desc", "mapping": {"text": "review"}},
    )
    assert resp.status_code == 201
    assert fake_service.calls[0]["description"] is None


def test_submit_upload_empty_name_is_422(client: TestClient, fake_service: _FakeService) -> None:
    """An empty name is a client error from the model's min_length bound."""
    resp = client.post(
        "/api/datasets/upload",
        json={"upload_id": "up-1", "name": "", "mapping": {"text": "review"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


def test_submit_upload_whitespace_name_is_422(
    client: TestClient, fake_service: _FakeService
) -> None:
    """A whitespace-only name is rejected by the endpoint; service not called."""
    resp = client.post(
        "/api/datasets/upload",
        json={"upload_id": "up-1", "name": "   ", "mapping": {"text": "review"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


def test_submit_upload_missing_name_is_422(client: TestClient, fake_service: _FakeService) -> None:
    resp = client.post("/api/datasets/upload", json={"upload_id": "up-1"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


def test_submit_upload_invalid_file_is_422(client: TestClient, fake_service: _FakeService) -> None:
    """An invalid staged file (service raises) → 422 with the parser's message."""
    fake_service.raise_invalid = "The file is larger than the 10 MB upload limit."

    resp = client.post(
        "/api/datasets/upload",
        json={"upload_id": "up-bad", "name": "Too big", "mapping": {"text": "review"}},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "The file is larger than the 10 MB upload limit."
