"""Markdown rendering for the extraction evaluation report.

Turns a scored :class:`evals.extraction.scorer.Report` into ``report.md`` with:

- a header noting whether AI-backed methods ran and the layout-coverage check;
- the automatic-choice threshold summary (Requirement 8.3), computed and
  reported (the failing CI gate that acts on it is Task 9.2);
- a per-method totals table (precision, recall, field accuracy, next-page
  detection, AI tokens, time) — Requirement 8.2;
- a per-page breakdown of the same metrics.

Kept separate from the runner so the formatting is easy to read and test.
"""

from __future__ import annotations

from evals.extraction.scorer import (
    ALL_METHODS,
    AUTO_PRECISION_THRESHOLD,
    AUTO_RECALL_THRESHOLD,
    METHOD_AUTO,
    VERDICT_ACCURACY_THRESHOLD,
    FieldAccuracy,
    MethodScore,
    Report,
    aggregate_method,
    aggregate_verdict,
    auto_meets_thresholds,
    verdict_meets_threshold,
)


def _pct(value: float | None) -> str:
    """Format a 0..1 ratio as a percentage, or ``—`` when not applicable."""
    return f"{value * 100:.1f}%" if value is not None else "—"


def _field(acc: FieldAccuracy) -> str:
    """Format a field accuracy as ``pct (correct/total)`` or ``—``."""
    if acc.total == 0:
        return "—"
    return f"{_pct(acc.value)} ({acc.correct}/{acc.total})"


def _next_page(correct: bool | None) -> str:
    if correct is None:
        return "—"
    return "✓" if correct else "✗"


def _secs(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _method_row(ms: MethodScore) -> str:
    """Render one method's metrics as a Markdown table row."""
    if not ms.applicable:
        note = ms.note or "n/a"
        return (
            f"| {ms.method} | _{note}_ | — | — | — | — | — | — | — | — |"
        )
    return (
        f"| {ms.method} "
        f"| {_pct(ms.precision)} "
        f"| {_pct(ms.recall)} "
        f"| {_field(ms.rating)} "
        f"| {_field(ms.date)} "
        f"| {_field(ms.author)} "
        f"| {_next_page(ms.next_page_correct)} "
        f"| {ms.extracted} "
        f"| {ms.tokens} "
        f"| {_secs(ms.seconds)} |"
    )


_TABLE_HEADER = (
    "| Method | Precision | Recall | Rating | Date | Author | Next page | "
    "Extracted | AI tokens | Time |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def _verdict_section(report: Report) -> list[str]:
    """Render the viability-verdict accuracy section (R3.14).

    Shows predicted vs. expected verdict per page and the overall accuracy over
    the scored pages against the 0.90 threshold. Offline, only the blocker pages
    (no AI) are scored, so the full-corpus gate is reported as not evaluated;
    live, every page is scored and the gate pass/fail is shown.
    """
    lines: list[str] = []
    passed, accuracy = verdict_meets_threshold(report)
    agg = aggregate_verdict(report)

    lines.append("## Viability verdict accuracy (dataset-ingestion Requirement 3.14)")
    lines.append("")
    if not report.include_ai:
        lines.append(
            "_Full-corpus gate not evaluated: this was an offline run, so only "
            "blocker pages (no AI call) were scored. Run with the live model "
            "(`--live`) to evaluate verdict accuracy across every page._"
        )
        lines.append("")
        lines.append(
            f"- Offline-scorable subset accuracy: **{_pct(agg.accuracy)}** "
            f"({agg.correct}/{agg.total} scored pages)"
        )
    else:
        lines.append(
            f"- Verdict accuracy: **{_pct(accuracy)}** "
            f"({agg.correct}/{agg.total} pages, threshold ≥ "
            f"{VERDICT_ACCURACY_THRESHOLD:.0%})"
        )
        lines.append(f"- Threshold met: **{'yes' if passed else 'no'}**")
    lines.append("")

    lines.append("| Page | Expected | Predicted | Correct |")
    lines.append("|---|---|---|---|")
    for v in report.verdicts:
        if not v.scored:
            predicted = f"_{v.note or 'not scored'}_"
            correct = "—"
        else:
            predicted = f"`{v.predicted}`"
            correct = "✓" if v.correct else "✗"
        lines.append(f"| {v.page} | `{v.expected}` | {predicted} | {correct} |")
    lines.append("")
    return lines


def render_report(report: Report, *, missing_tags: set[str] | None = None) -> str:
    """Render the full report to a Markdown string."""
    lines: list[str] = []
    lines.append("# Extraction evaluation report")
    lines.append("")
    mode = "live (AI-backed methods scored)" if report.include_ai else "offline (no live AI)"
    lines.append(f"- Run mode: **{mode}**")
    lines.append(f"- Pages scored: **{len(report.pages)}**")

    missing = missing_tags or set()
    if missing:
        lines.append(f"- ⚠️ Missing required layout coverage: {sorted(missing)}")
    else:
        lines.append("- Layout coverage: **all required layout types present**")
    lines.append("")

    # --- Threshold summary (Requirement 8.3) -------------------------------
    passed, precision, recall = auto_meets_thresholds(report)
    lines.append("## Automatic choice vs. thresholds (Requirement 8.3)")
    lines.append("")
    if not report.include_ai:
        lines.append(
            "_Not evaluated: this was an offline run, so the automatic choice "
            "was not scored. Run with the live model (`--live`) to evaluate._"
        )
    else:
        lines.append(
            f"- Precision on `will_work` pages: **{_pct(precision)}** "
            f"(threshold ≥ {AUTO_PRECISION_THRESHOLD:.0%})"
        )
        lines.append(
            f"- Recall on `will_work` pages: **{_pct(recall)}** "
            f"(threshold ≥ {AUTO_RECALL_THRESHOLD:.0%})"
        )
        lines.append(f"- Thresholds met: **{'yes' if passed else 'no'}**")
    lines.append("")

    # --- Verdict accuracy (dataset-ingestion Requirement 3.14) -------------
    lines.extend(_verdict_section(report))

    # --- Totals per method -------------------------------------------------
    lines.append("## Totals by method")
    lines.append("")
    lines.append(_TABLE_HEADER)
    for method in ALL_METHODS:
        lines.append(_method_row(aggregate_method(report, method)))
    lines.append("")

    # --- Per-page breakdown ------------------------------------------------
    lines.append("## Per-page scores")
    lines.append("")
    for page in report.pages:
        tags = ", ".join(page.tags) if page.tags else "—"
        lines.append(f"### {page.page}")
        lines.append("")
        lines.append(f"- Verdict: `{page.verdict}` · Tags: {tags}")
        lines.append("")
        lines.append(_TABLE_HEADER)
        for method in ALL_METHODS:
            ms = page.methods.get(method)
            if ms is None:
                ms = MethodScore(method=method, applicable=False, note="not scored")
            lines.append(_method_row(ms))
        lines.append("")

    # Highlight the automatic choice note at the end for quick scanning.
    lines.append(
        f"> The default extraction strategy should be the best-scoring method in "
        f"this report (Requirement 8.5). The automatic choice is `{METHOD_AUTO}`."
    )
    lines.append("")
    return "\n".join(lines)
