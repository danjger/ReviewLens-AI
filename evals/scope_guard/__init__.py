"""Scope-guard evaluation suite (guardrailed-chat, Requirement 8).

A fixed fixture dataset ("Acme CRM on G2") and a labeled set of at least 60
questions (``cases.yaml``) that exercise the chat's scope guard across every
category: in-scope, other-platform, world-knowledge/weather, competitor facts,
unrelated tasks, borderline (answerable from the reviews), and prompt-injection
attempts — including one planted injection *inside* a review's text
(Requirement 8.1).

Layout (mirrors ``evals.extraction``):

- ``reviews/v1.json`` — the fixture Corpus, shaped exactly like the
  ``reviews/v{n}.json`` document review-analysis writes
  (``{dataset_id, version, generated_at, entity, pages[], reviews[]}``), so it
  loads through :func:`app.chat.corpus` unchanged. Contains the planted
  injection review ``r_0010``.
- ``dataset.json`` — the small amount of dataset metadata that does **not** live
  in ``reviews/v{n}.json`` (``platform`` and ``original_url``), which the chat
  system prompt's SCOPE line needs.
- ``cases.yaml`` — the labeled questions and their expected behavior.
- ``cases.py`` — the pure, offline loader: parses ``cases.yaml`` into typed
  :class:`~evals.scope_guard.cases.Case` objects and loads the fixture
  :class:`~app.chat.corpus.Corpus`.

This task (7.1) is **data + loader only**. The grader is Task 7.2, the CI job is
Task 7.3, and the live model run is Task 7.4. Nothing here calls the AI; the
loader reads files only, so its unit test runs with no API key.
"""
