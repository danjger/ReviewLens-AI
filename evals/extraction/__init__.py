"""Extraction evaluation suite (review-extraction, Requirement 8).

A labelled set of saved review pages (``pages/`` + ``labels.yaml``) and a scorer
(:mod:`evals.extraction.scorer`) that measures each extraction method and the
automatic choice on precision, recall, field accuracy, next-page detection, AI
tokens, and time. :mod:`evals.extraction.run` writes ``report.md`` and is the
on-demand / CI entry point.

The offline methods (``structured``, ``selectors``) run with no AI; the AI-backed
methods (``ai_direct``, ``auto``) run only under the live model (Task 9.4). See
``run.py`` for how it is invoked.
"""
