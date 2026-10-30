import { z } from "zod";

const dependencyState = z.enum(["up", "down"]);

export const readinessSchema = z.object({
  status: z.enum(["ready", "degraded"]),
  dependencies: z.object({
    postgres: dependencyState,
    redis: dependencyState,
  }),
});

export type Readiness = z.infer<typeof readinessSchema>;

export type ReadinessResult =
  | { kind: "ready"; readiness: Readiness }
  | { kind: "degraded"; readiness: Readiness }
  | { kind: "unreachable"; reason: string };

/**
 * Queries /health/ready. HTTP 200 and 503 both carry a readiness body; any
 * other outcome (network failure, wrong status, malformed body) is "unreachable"
 * and is never reported as healthy.
 */
export async function fetchReadiness(
  baseUrl: string,
  signal?: AbortSignal,
): Promise<ReadinessResult> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/health/ready`, { cache: "no-store", signal });
  } catch (error) {
    if (signal?.aborted) throw error;
    return { kind: "unreachable", reason: "Network request failed" };
  }

  if (response.status !== 200 && response.status !== 503) {
    return { kind: "unreachable", reason: `Unexpected response (HTTP ${response.status})` };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "unreachable", reason: "Response was not valid JSON" };
  }

  const parsed = readinessSchema.safeParse(body);
  if (!parsed.success) {
    return { kind: "unreachable", reason: "Response did not match the readiness contract" };
  }

  const allUp = Object.values(parsed.data.dependencies).every((state) => state === "up");
  if (response.status === 200 && parsed.data.status === "ready" && allUp) {
    return { kind: "ready", readiness: parsed.data };
  }
  return { kind: "degraded", readiness: parsed.data };
}
