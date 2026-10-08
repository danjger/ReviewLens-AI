/**
 * ThemesList — the recurring-themes list on the detail page
 * (ingestion-summary task 3.3, Requirement 2.4).
 *
 * The Detail Page shows up to 8 recurring themes with mention counts and
 * sentiment lean (Requirement 2.4; design "Components and Interfaces": label,
 * mention count, and a sentiment-lean indicator — icon plus text, not colour
 * alone).
 *
 * **Data source.** The themes are `metrics.themes`, the list the review-analysis
 * metrics stage emits (`app/handlers/metrics.py::_themes_payload`): each entry
 * is `{label, mentions, lean}` where `lean` is one of
 * `"positive" | "neutral" | "negative"` (the design's `extract_themes` shape).
 * This component mirrors those field names exactly; it does not invent a shape.
 * It is read off the active-version `metrics` dict (the same object the other
 * metrics components read), so the detail page passes the single
 * `dataset.metrics` object to every metrics component.
 *
 * **Cap at 8 (Requirement 2.4).** The producer already caps themes at eight,
 * but this component defensively renders at most {@link MAX_THEMES} so a longer
 * list (e.g. from an older metrics blob) never overflows the panel.
 *
 * **Sentiment lean shown as text, not colour alone (design + accessibility
 * rule).** Each theme's lean is conveyed with an icon *and* a text label
 * ("Positive" / "Neutral" / "Negative"), so the information is never carried by
 * colour or the icon glyph alone. The icon is `aria-hidden`; the text is the
 * programmatically-available signal.
 *
 * **Empty / missing.** When there are no themes — the whole `metrics` object is
 * null, the `themes` key is missing, or it is an empty list (e.g. themes were
 * omitted with a warning upstream) — the component renders a small empty state
 * rather than an empty list, consistent with the other panels.
 */
import type { DatasetDetail } from "../api/datasets";

export interface ThemesListProps {
  /** The active-version metrics dict, or null until a version lands. */
  metrics: DatasetDetail["metrics"];
}

/** A theme's sentiment lean (review-analysis `extract_themes` output). */
type ThemeLean = "positive" | "neutral" | "negative";

/** The valid lean values, used to validate the (untyped) metrics field. */
const LEANS: readonly ThemeLean[] = ["positive", "neutral", "negative"] as const;

/** One recurring theme (review-analysis `metrics.themes` entry). */
interface Theme {
  label: string;
  mentions: number;
  lean: ThemeLean;
}

/** Up to 8 themes are shown (Requirement 2.4); the producer caps at the same. */
const MAX_THEMES = 8;

/** The empty-state label, matching the other panels' wording. */
const EMPTY_LABEL = "No recurring themes";

/** Icon glyph (decorative) and visible text for each sentiment lean. */
const LEAN_PRESENTATION: Record<ThemeLean, { icon: string; text: string }> = {
  positive: { icon: "▲", text: "Positive" },
  neutral: { icon: "■", text: "Neutral" },
  negative: { icon: "▼", text: "Negative" },
};

/** True when *value* is a finite, non-negative number. */
function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/** True when *value* is one of the three valid lean strings. */
function isLean(value: unknown): value is ThemeLean {
  return typeof value === "string" && (LEANS as readonly string[]).includes(value);
}

/**
 * Read `metrics.themes` into a validated, capped `Theme[]`.
 *
 * Each raw entry is kept only when it has a non-empty `label`; its `mentions`
 * default to `0` when missing/invalid and its `lean` defaults to `"neutral"`
 * when missing/invalid, so a slightly-degraded entry still renders rather than
 * dropping out. Entries without a usable label are skipped. The result is
 * capped to {@link MAX_THEMES}. Returns an empty array when there are no themes.
 */
function readThemes(metrics: DatasetDetail["metrics"]): Theme[] {
  if (metrics == null) return [];
  const raw = (metrics as Record<string, unknown>).themes;
  if (!Array.isArray(raw)) return [];

  const themes: Theme[] = [];
  for (const entry of raw) {
    if (entry == null || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    const label = record.label;
    if (typeof label !== "string" || label.trim().length === 0) continue;
    themes.push({
      label: label.trim(),
      mentions: isCount(record.mentions) ? record.mentions : 0,
      lean: isLean(record.lean) ? record.lean : "neutral",
    });
    if (themes.length >= MAX_THEMES) break;
  }
  return themes;
}

export default function ThemesList({ metrics }: ThemesListProps) {
  const themes = readThemes(metrics);

  if (themes.length === 0) {
    return (
      <section
        className="themes-list themes-list--empty"
        data-testid="themes-list"
        data-available="false"
        aria-label="Recurring themes"
      >
        <h2 className="themes-list__heading">Recurring themes</h2>
        <p className="themes-list__empty" data-testid="themes-empty">
          {EMPTY_LABEL}
        </p>
      </section>
    );
  }

  return (
    <section
      className="themes-list"
      data-testid="themes-list"
      data-available="true"
      aria-label="Recurring themes"
    >
      <h2 className="themes-list__heading">Recurring themes</h2>
      <ul className="themes-list__items" data-testid="themes-list-items">
        {themes.map((theme, index) => {
          const lean = LEAN_PRESENTATION[theme.lean];
          return (
            <li
              // Labels can repeat across versions; the index keeps the key
              // stable for this render (the list is small and static).
              key={`${theme.label}-${index}`}
              className="themes-list__item"
              data-testid="theme-item"
              data-lean={theme.lean}
            >
              <span className="themes-list__label" data-testid="theme-label">
                {theme.label}
              </span>
              <span className="themes-list__mentions" data-testid="theme-mentions">
                {theme.mentions.toLocaleString()}
                <span className="themes-list__mentions-unit"> mentions</span>
              </span>
              <span
                className={`themes-list__lean themes-list__lean--${theme.lean}`}
                data-testid="theme-lean"
                data-lean={theme.lean}
              >
                <span className="themes-list__lean-icon" aria-hidden="true">
                  {lean.icon}
                </span>
                <span className="themes-list__lean-text" data-testid="theme-lean-text">
                  {lean.text}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
