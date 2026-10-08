/**
 * EntityCard — the "what is being reviewed" card on the detail page
 * (ingestion-summary task 3.3, Requirement 2.3).
 *
 * The Detail Page shows the identified entity name, category, and description,
 * and flags them when confidence is low (Requirement 2.3; design "Components
 * and Interfaces": name, category, description, and a "low confidence" chip).
 *
 * **Data source.** The entity profile is the `entity` block produced by the
 * review-analysis profiler (`app/worker/ai/profile.py::EntityProfile` →
 * `app/handlers/metrics.py::_build_reviews_doc`), whose shape is
 * `{name, category, description, confidence}` with `confidence` one of
 * `"high" | "low"`. This component mirrors those field names exactly; it does
 * not invent a shape. It is read off the active-version `metrics` dict under
 * the `entity` key (the same `metrics` object {@link MetricsPanel} and
 * {@link RatingDistributionChart} read), so the detail page can pass the single
 * `dataset.metrics` object to every metrics component.
 *
 * **Low-confidence flag (Requirement 2.3).** When the profiler could not
 * confidently identify the entity it marks the profile `confidence: "low"`
 * (profile.py, Requirement 4.2). This card then shows a "Low confidence" chip
 * next to the name so the analyst knows to treat the identification with care.
 * The chip carries its own meaning as text (not colour alone), consistent with
 * the accessibility rule the rest of the summary follows.
 *
 * **Not available.** When there is no entity profile — the whole `metrics`
 * object is null, the `entity` block is missing, or it carries no usable name —
 * the card renders "Not available" rather than an empty shell, matching how the
 * other panels handle a missing metric (Requirement 2.5 wording).
 */
import type { DatasetDetail } from "../api/datasets";

export interface EntityCardProps {
  /** The active-version metrics dict, or null until a version lands. */
  metrics: DatasetDetail["metrics"];
}

/** The entity profile shape from review-analysis (`reviews/v{n}.json` `entity`). */
interface EntityProfile {
  name: string;
  category: string;
  description: string;
  /** `"low"` when the profiler fell back (Requirement 4.2); else `"high"`. */
  lowConfidence: boolean;
}

/** The label shown when there is no entity profile (Requirement 2.5 wording). */
const NOT_AVAILABLE = "Not available";

/** True when *value* is a non-empty string once trimmed. */
function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Read the `entity` block off the metrics dict, or `null` when it is absent or
 * unusable.
 *
 * An entity is "usable" only when it carries a non-empty `name` — the profiler
 * always sets a name (the page title / upload name is used as the fallback
 * label), so a missing name means there is no profile to show at all. The
 * `confidence` field is normalised to a boolean `lowConfidence`: it is low only
 * when the stored value is exactly `"low"` (the profiler's only non-high value,
 * Requirement 4.2); anything else is treated as confident.
 */
function readEntity(metrics: DatasetDetail["metrics"]): EntityProfile | null {
  if (metrics == null) return null;
  const raw = (metrics as Record<string, unknown>).entity;
  if (raw == null || typeof raw !== "object") return null;

  const record = raw as Record<string, unknown>;
  if (!nonEmptyString(record.name)) return null;

  return {
    name: record.name.trim(),
    category: nonEmptyString(record.category) ? record.category.trim() : "",
    description: nonEmptyString(record.description) ? record.description.trim() : "",
    lowConfidence: record.confidence === "low",
  };
}

/** The "Not available" empty state, matching the other panels' wording. */
function NotAvailable() {
  return (
    <section
      className="entity-card entity-card--empty"
      data-testid="entity-card"
      data-available="false"
      aria-label="Identified entity"
    >
      <h2 className="entity-card__heading">Identified entity</h2>
      <p className="entity-card__not-available" data-testid="entity-not-available">
        {NOT_AVAILABLE}
      </p>
    </section>
  );
}

export default function EntityCard({ metrics }: EntityCardProps) {
  const entity = readEntity(metrics);
  if (entity == null) {
    return <NotAvailable />;
  }

  return (
    <section
      className="entity-card"
      data-testid="entity-card"
      data-available="true"
      data-confidence={entity.lowConfidence ? "low" : "high"}
      aria-label="Identified entity"
    >
      <h2 className="entity-card__heading">Identified entity</h2>

      <div className="entity-card__name-row">
        <span className="entity-card__name" data-testid="entity-name">
          {entity.name}
        </span>
        {entity.lowConfidence && (
          <span
            className="entity-card__chip entity-card__chip--low-confidence"
            data-testid="entity-low-confidence"
            title="The product could not be confidently identified from the page."
          >
            Low confidence
          </span>
        )}
      </div>

      {entity.category && (
        <p className="entity-card__category" data-testid="entity-category">
          {entity.category}
        </p>
      )}

      {entity.description && (
        <p className="entity-card__description" data-testid="entity-description">
          {entity.description}
        </p>
      )}
    </section>
  );
}
