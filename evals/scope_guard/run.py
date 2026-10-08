"""Scope-guard evaluation runner — grades the cases and writes ``report.md``.

This is the on-demand and CI entry point for the guardrail evaluation suite
(``guardrailed-chat`` Requirement 8). It loads the labeled cases and the fixture
dataset, grades every case through the real chat flow (assemble → main model →
post-process → LLM-judge), writes a Markdown report next to this module, and
reports whether the metrics meet the Requirement 8.3 thresholds.

How it is invoked
-----------------

**As a script**::

    python evals/scope_guard/run.py                 # offline: structure only, no scoring
    python evals/scope_guard/run.py --live          # grades every case (needs a key)
    python evals/scope_guard/run.py --out report.md  # choose the report path

Run it from the repo root (or anywhere). The script adds the repo root and
``backend/`` to ``sys.path`` so ``import app`` and ``import evals`` work without
installation, exactly like :mod:`evals.extraction.run`.

**Under pytest (live run, Task 7.4)**::

    cd backend && uv run pytest ../evals -v -m live_ai   # per structure.md (repo-root /evals)

The live pytest entry point lives in :mod:`evals.scope_guard.test_run_live`
(marked ``live_ai`` so the default backend run skips it). It writes the report
and asserts the thresholds — this is what Task 7.4 runs once the live model is
available and ``system_v1.md`` is tuned.

Offline vs live
---------------

- Offline (default, no key): grading a case needs the live model, so the offline
  run scores nothing. The report is still written showing the run was offline and
  that the thresholds were not evaluated — the structure is visible without a
  key, mirroring the extraction runner.
- Live (``--live`` / the ``live_ai`` pytest marker): every case is graded and the
  four metrics are evaluated against their thresholds.
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

from evals.scope_guard import grader  # noqa: E402 - after sys.path setup
from evals.scope_guard.report import render_report  # noqa: E402

#: Default location of the written report (beside this module).
DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "report.md"


def run_evaluation(*, include_ai: bool, out: Path = DEFAULT_REPORT_PATH) -> grader.Report:
    """Grade the cases, write ``report.md``, and return the report.

    :param include_ai: Grade every case with the live model and judge. ``False``
        keeps the run fully offline (nothing scored), writing a structure-only
        report.
    :param out: Where to write the Markdown report.
    :returns: The computed :class:`~evals.scope_guard.grader.Report`.
    """
    report = grader.run_cases(include_ai=include_ai)
    out.write_text(render_report(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (always 0 for a scored run)."""
    parser = argparse.ArgumentParser(description="Run the scope-guard evaluation suite.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Grade every case with the live model and judge (needs ANTHROPIC_API_KEY).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to write the Markdown report (default: evals/scope_guard/report.md).",
    )
    args = parser.parse_args(argv)

    report = run_evaluation(include_ai=args.live, out=args.out)

    print(f"Wrote {args.out}")  # noqa: T201 - CLI feedback
    if report.include_ai:
        m = report.metrics
        thresholds = grader.meets_thresholds(m)
        print(  # noqa: T201
            f"correct_decline={_fmt(m.correct_decline_rate)} "
            f"false_decline={_fmt(m.false_decline_rate)} "
            f"injection_successes={m.injection_successes} "
            f"citation_validity={_fmt(m.citation_validity)} "
            f"thresholds_met={thresholds.passed}"
        )
    else:
        print("Offline run: no cases scored; thresholds not evaluated (use --live).")  # noqa: T201
    return 0


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
