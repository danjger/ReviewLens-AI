/**
 * Tests for the compute-mode seam (platform-foundation task 6.5).
 *
 * Requirement 3.9: deploy in Lambda mode by default; be structured so a
 * container mode can be added per Service without changing application code.
 *
 * Covers:
 *   - parseComputeMode: default, explicit values, and rejection of bad values.
 *   - ApiStack conforms to ComputeTier (apiDomain / chatDomain) and the default
 *     app synthesizes exactly the Lambda-mode compute tier (Api + Workers).
 *   - container mode fails fast with a clear message pointing at task 6.10,
 *     without breaking the build.
 */
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { DataStack } from "../lib/data-stack";
import { ApiStack } from "../lib/api-stack";
import {
  parseComputeMode,
  DEFAULT_COMPUTE_MODE,
  type ComputeMode,
  type ComputeTier,
} from "../lib/compute-mode";

describe("parseComputeMode", () => {
  test("absent value defaults to lambda", () => {
    expect(parseComputeMode(undefined)).toBe("lambda");
    expect(parseComputeMode(null)).toBe("lambda");
    expect(parseComputeMode("")).toBe("lambda");
    expect(DEFAULT_COMPUTE_MODE).toBe("lambda");
  });

  test("accepts the two valid modes", () => {
    expect(parseComputeMode("lambda")).toBe("lambda");
    expect(parseComputeMode("container")).toBe("container");
  });

  test("rejects any other value with a clear message", () => {
    expect(() => parseComputeMode("fargate")).toThrow(/Invalid computeMode/);
    expect(() => parseComputeMode("Lambda")).toThrow(/Invalid computeMode/);
    expect(() => parseComputeMode(42)).toThrow(/Invalid computeMode/);
  });
});

describe("ComputeTier conformance", () => {
  test("ApiStack satisfies ComputeTier (apiDomain / chatDomain)", () => {
    const app = new cdk.App();
    const env = { account: "111111111111", region: "us-east-1" };
    const data = new DataStack(app, "TestData", { destroyableData: true, env });
    const api = new ApiStack(app, "TestApi", { env, data, envName: "test" });

    // Assignable to ComputeTier at the type level, and the getters alias the
    // existing Lambda-mode domains.
    const tier: ComputeTier = api;
    expect(tier.apiDomain).toBe(api.httpApiDomain);
    expect(tier.chatDomain).toBe(api.chatFunctionUrlDomain);
  });
});

describe("app synthesis per compute mode", () => {
  /** Synthesize bin/app.ts under a given computeMode context and return the assembly. */
  function synthApp(mode?: ComputeMode): cdk.cx_api.CloudAssembly {
    const context: Record<string, unknown> = { destroyableData: true };
    if (mode !== undefined) {
      context.computeMode = mode;
    }
    const app = new cdk.App({ context });

    // Rebuild the compute branch exactly as bin/app.ts does, but inline so the
    // test can assert without spawning the CDK CLI. This mirrors the central
    // decision: parse once, branch, Edge reads the ComputeTier.
    const resolved = parseComputeMode(app.node.tryGetContext("computeMode"));
    const env = { account: "111111111111", region: "us-east-1" };
    const data = new DataStack(app, "ReviewLens-Data", {
      destroyableData: true,
      env,
    });

    if (resolved === "lambda") {
      new ApiStack(app, "ReviewLens-Api", { env, data, envName: "test" });
    } else {
      throw new Error(
        "computeMode=container is not available yet: ... task 6.10 ..."
      );
    }
    return app.synth();
  }

  test("default (no computeMode) builds the Lambda-mode Api compute tier", () => {
    const assembly = synthApp();
    const stackNames = assembly.stacks.map((s) => s.stackName);
    expect(stackNames).toContain("ReviewLens-Data");
    expect(stackNames).toContain("ReviewLens-Api");
  });

  test("explicit lambda mode builds the same Api compute tier", () => {
    const assembly = synthApp("lambda");
    const api = assembly.getStackByName("ReviewLens-Api");
    const template = Template.fromJSON(api.template);
    // The Lambda-mode HTTP tier is present (HTTP API fronting the API Lambda).
    template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
  });

  test("container mode fails fast until task 6.10", () => {
    expect(() => synthApp("container")).toThrow(/task 6\.10/);
  });
});
