"""Extraction evaluation runner — scores the methods and writes ``report.md``.

This is the on-demand and CI entry point for the extraction evaluation suite
(``review-extraction`` Requirement 8). It loads the labelled pages, scores every
method and the automatic choice, writes a Markdown report next to this module,
and reports whether the automatic choice meets the Requirement 8.3 thresholds.

How it is invoked
-----------------

**As a script (offline by default)**::

    python evals/extraction/run.py                 # offline: structured + selectors only
    python evals/extraction/run.py --live          # also runs ai_direct + auto (needs a key)
    python evals/extraction/run.py --out report.md  # choose the report path

Run it from the repo root (or anywhere, as long as ``backend`` and the repo root
are importable). The script adds the repo root and ``backend/`` to ``sys.path``
so ``import app`` and ``import evals`` work without installation.

**Under pytest (live run, Task 9.4)**::

    cd backend && uv run pytest ../evals -v -m live_ai   # per structure.md (repo-root /evals)

The ``test_live_evaluation`` function here is marked ``live_ai`` so it is skipped
by the default backend test run and only executes when live AI is explicitly
selected (and an ``ANTHROPIC_API_KEY`` is configured). It writes the report and
asserts the thresholds — this is what Task 9.4 runs.

Offline vs live
---------------

- Offline (default, no key): ``structured`` and ``selectors`` are scored fully;
  ``ai_direct`` and ``auto`` are marked "skipped (no live AI)". The report is
  still written so the structure and the offline numbers are visible.
- Live (``--live`` / the ``live_ai`` pytest marker): all four are scored and the
  automatic-choice thresholds are evaluated.

Makefile note
-------------

``make eval`` currently runs ``cd backend && uv run pytest evals/ -v -m live_ai``,
but per ``structure.md`` the eval suite lives at the **repo root** ``/evals``,
not ``backend/evals``. This runner is importable and runnable from the repo root
either way; the Makefile path is left unchanged here (changing it is a judgement
call flagged in the task report). To run the live suite today, invoke pytest
against the repo-root path: ``cd backend && uv run pytest ../evals -v -m live_ai``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``app`` (backend) and ``evals`` importable when run as a script from any
# working directory, without installing the packages.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
for _path in (_REPO_ROOT, _BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evals.extraction import scorer  # noqa: E402 - after sys.path setup
from evals.extraction.labels import load_labeled_pages, missing_required_tags  # noqa: E402
from evals.extraction.report import render_report  # noqa: E402

#: Default location of the written report (beside this module).
DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "report.md"


def run_evaluation(*, include_ai: bool, out: Path = DEFAULT_REPORT_PATH) -> scorer.Report:
    """Load pages, score them, write ``report.md``, and return the report.

    :param include_ai: Score the AI-backed methods (``ai_direct``, ``auto``) too.
        ``False`` keeps the run fully offline and deterministic.
    :param out: Where to write the Markdown report.
    :returns: The computed :class:`~evals.extraction.scorer.Report`.
    """
    pages = load_labeled_pages()
    missing = missing_required_tags(pages)
    report = scorer.score_pages(pages, include_ai=include_ai)
    out.write_text(render_report(report, missing_tags=missing), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (always 0 for a scored run)."""
    parser = argparse.ArgumentParser(description="Run the extraction evaluation suite.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also score the AI-backed methods (needs ANTHROPIC_API_KEY).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to write the Markdown report (default: evals/extraction/report.md).",
    )
    args = parser.parse_args(argv)

    report = run_evaluation(include_ai=args.live, out=args.out)
    passed, precision, recall = scorer.auto_meets_thresholds(report)
    verdict_passed, verdict_accuracy = scorer.verdict_meets_threshold(report)
    verdict_agg = scorer.aggregate_verdict(report)

    print(f"Wrote {args.out}")  # noqa: T201 - CLI feedback
    if report.include_ai:
        print(  # noqa: T201
            f"auto precision={_fmt(precision)} recall={_fmt(recall)} "
            f"thresholds_met={passed}"
        )
        print(  # noqa: T201
            f"verdict accuracy={_fmt(verdict_accuracy)} "
            f"({verdict_agg.correct}/{verdict_agg.total}) "
            f"threshold_met={verdict_passed}"
        )
    else:
        print("Offline run: ai_direct / auto skipped; thresholds not evaluated.")  # noqa: T201
        print(  # noqa: T201
            f"Offline verdict accuracy (blocker subset): "
            f"{_fmt(verdict_agg.accuracy)} "
            f"({verdict_agg.correct}/{verdict_agg.total}); "
            f"full-corpus gate needs --live."
        )
    return 0


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
