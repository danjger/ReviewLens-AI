"""The guardrailed-chat domain package.

This package holds the chat domain modules (``prompts``, ``corpus``,
``assembly``, ``precheck``, ``postprocess``, ``sse``) and the streaming FastAPI
service in :mod:`app.chat.service`.

The service app is re-exported here as ``app`` so that ``app.chat:app`` — the
entry point used by ``docker-compose`` (the ``chat`` service), the backend
``Dockerfile`` CMD override, and the CDK ``ChatFunction`` — resolves to the
FastAPI application. Because ``import app.chat`` resolves to this package (not a
sibling ``app/chat.py`` module), the app must live inside the package; keeping
it in ``service.py`` and re-exporting it here keeps that single entry point
working while leaving the domain modules importable in the usual way
(``from app.chat.corpus import Corpus``).
"""

from __future__ import annotations

from app.chat.service import app

__all__ = ["app"]
