/**
 * Compute mode seam (platform-foundation task 6.5).
 *
 * Requirement 3.9: "The infrastructure code SHALL deploy in Lambda mode by
 * default and SHALL be structured so that a container mode can be added per
 * Service without changing application code."
 *
 * The compute mode is a SINGLE, CENTRAL decision read once from the
 * `computeMode` context value (default `lambda`). The switch lives entirely in
 * infrastructure (see the design's Technology-choices table: "One `cdk deploy`;
 * the switch is in infrastructure only"). Application code never learns which
 * mode it runs in — the same `backend`/`workers` images serve both:
 *
 *   - `lambda`    (default): ApiStack + WorkersStack provide the compute, and
 *                 Edge fronts the HTTP API + chat Function URL.
 *   - `container` (later):   the optional ContainersStack (ECS Fargate, task
 *                 6.10) provides the compute behind an ALB, and Edge fronts
 *                 the ALB. The Data/Edge/Frontend/Cost/TestFixtures stacks are
 *                 compute-mode-agnostic and always deploy.
 *
 * To let both compute stacks be interchangeable at the one wiring point in
 * bin/app.ts, each exposes a `ComputeTier`: the minimal, typed surface the Edge
 * stack needs (the API origin domain and the chat origin domain). ApiStack
 * already satisfies it today via `httpApiDomain` / `chatFunctionUrlDomain`; the
 * future ContainersStack will satisfy it with its ALB domain(s). Edge wires
 * whichever is active without caring which mode produced it.
 */

/** The two supported compute modes. `lambda` is the default and only one built today. */
export type ComputeMode = "lambda" | "container";

/** The default compute mode when the `computeMode` context value is absent. */
export const DEFAULT_COMPUTE_MODE: ComputeMode = "lambda";

/** The complete set of valid compute-mode strings, for validation + messaging. */
export const COMPUTE_MODES: readonly ComputeMode[] = ["lambda", "container"];

/**
 * The minimal, typed surface the Edge stack needs from whichever compute stack
 * is active. Both the Lambda-mode ApiStack and the future container-mode
 * ContainersStack conform to this, so bin/app.ts can wire Edge to either
 * without branching on the concrete type.
 *
 * Both fields are host-only domain names (no scheme, no trailing slash):
 *   - `apiDomain`  → the `/api/*` CloudFront origin
 *   - `chatDomain` → the `/api/chat/*` CloudFront origin
 *
 * In Lambda mode these are the API Gateway HTTP API domain and the chat
 * Function URL domain. In container mode they are the ALB domain(s).
 */
export interface ComputeTier {
  /** Host-only domain of the API origin (API Gateway HTTP API, or ALB). */
  readonly apiDomain: string;
  /** Host-only domain of the chat origin (chat Function URL, or ALB). */
  readonly chatDomain: string;
}

/**
 * Parse and validate the `computeMode` context value.
 *
 * Reads `app.node.tryGetContext("computeMode")` at the call site and passes the
 * raw value here. An absent value resolves to {@link DEFAULT_COMPUTE_MODE}
 * (`lambda`), so the default `cdk synth` / `cdk deploy` is always Lambda mode.
 * Any value other than `lambda` or `container` is a configuration error and
 * throws with a clear message.
 *
 * @param raw the raw context value (typically `string | undefined`).
 * @returns the validated {@link ComputeMode}.
 * @throws if `raw` is set to anything other than `lambda` or `container`.
 */
export function parseComputeMode(raw: unknown): ComputeMode {
  if (raw === undefined || raw === null || raw === "") {
    return DEFAULT_COMPUTE_MODE;
  }
  if (raw === "lambda" || raw === "container") {
    return raw;
  }
  throw new Error(
    `Invalid computeMode context value: ${JSON.stringify(raw)}. ` +
      `Expected one of ${COMPUTE_MODES.map((m) => `"${m}"`).join(" | ")}. ` +
      `Set it in cdk.json context or pass -c computeMode=<mode>.`
  );
}
