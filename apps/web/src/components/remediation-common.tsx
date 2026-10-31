"use client";

import { ApiError, type RemediationStatus } from "@/lib/api";

export const ACTIVE_STATUSES = new Set<RemediationStatus>(["drafting", "policy_check", "sandbox_queued", "sandbox_running", "awaiting_approval", "approved", "publishing"]);
export const RETRYABLE_STATUSES = new Set<RemediationStatus>(["failed", "stale", "policy_blocked", "verification_failed", "cancelled"]);
export const IN_FLIGHT_STATUSES = new Set<RemediationStatus>(["drafting", "policy_check", "sandbox_queued", "sandbox_running", "publishing"]);

const labels: Record<RemediationStatus, string> = {
  drafting: "Drafting", policy_check: "Policy check", policy_blocked: "Blocked by policy", sandbox_queued: "Sandbox queued",
  sandbox_running: "Sandbox running", verification_failed: "Verification failed", awaiting_approval: "Awaiting approval",
  approved: "Approved", publishing: "Publishing", pull_request_created: "Pull request created", rejected: "Rejected",
  stale: "Stale", failed: "Failed", cancelled: "Cancelled",
};

export function statusLabel(status: RemediationStatus): string { return labels[status]; }

export function RemediationStatusBadge({status}: {status: RemediationStatus}) {
  const tone = ["pull_request_created", "approved"].includes(status) ? "repo-active"
    : ["policy_blocked", "verification_failed", "failed", "stale", "rejected"].includes(status) ? "repo-failed" : "repo-syncing";
  return <span className={`status-badge ${tone}`}>{statusLabel(status)}</span>;
}

export function shortHash(value: string | null | undefined): string { return value ? value.slice(0, 12) : "—"; }

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return `${error.message}. The latest state has been loaded.`;
    if (error.status === 503 || error.status === 502) return "The repository provider could not be reached, so nothing was changed. Try again shortly.";
    return error.message;
  }
  return error instanceof Error ? error.message : "Request failed.";
}

export const NO_AUTO_MERGE = "OpsPilot never merges, approves, or deploys a pull request. A human must review and merge it through your normal process.";
