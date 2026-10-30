"use client";

import { useState } from "react";
import { ApiError, isProviderUnavailable } from "@/lib/api";

export function ObsFailure({error, what}: {error: unknown; what: string}) {
  if (isProviderUnavailable(error)) {
    return <section className="panel provider-unavailable" role="alert"><h3>Service unavailable</h3><p>The observability service could not be reached, so {what} cannot be shown right now. No data has been changed. Try again shortly.</p></section>;
  }
  return <section className="panel error" role="alert">{what[0]?.toUpperCase()}{what.slice(1)} could not be loaded.</section>;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return `${error.message} (not available on this deployment)`;
    if (error.status === 429) return `Rate limited: ${error.message}`;
    if (error.status === 503) return `Service unavailable: ${error.message}`;
    return error.message;
  }
  return error instanceof Error ? error.message : "Unexpected error";
}

export function formatMs(value: number | null): string {
  return value === null ? "—" : value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function statusClass(status: string): string {
  if (["processed", "active", "ok", "replayed"].includes(status)) return "obs-ok";
  if (["failed", "dead_lettered", "failing", "disabled"].includes(status)) return "obs-bad";
  if (["duplicate", "received", "published", "normalized", "throttled"].includes(status)) return "obs-warn";
  return "";
}

export function StatusPill({status}: {status: string}) {
  return <span className={`status-badge ${statusClass(status)}`}>{status.replaceAll("_", " ")}</span>;
}

/** Renders a one-time secret. The secret lives only in the caller's component state. */
export function SecretReveal({secret, heading, onDismiss}: {secret: string; heading: string; onDismiss: () => void}) {
  const [copied, setCopied] = useState<"" | "yes" | "failed">("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied("yes");
    } catch {
      setCopied("failed");
    }
  }
  return <section className="panel secret-reveal" role="alert" aria-label="One-time webhook secret">
    <h3>{heading}</h3>
    <p className="notice"><strong>Copy this secret now. It will not be shown again.</strong> OpsPilot stores only an encrypted copy and cannot display it later. If you lose it, rotate the secret.</p>
    <code className="secret-value" data-testid="webhook-secret">{secret}</code>
    <div className="review-actions">
      <button type="button" className="button primary" onClick={() => void copy()}>Copy secret</button>
      <button type="button" className="button" onClick={onDismiss}>I have saved it, dismiss</button>
    </div>
    {copied === "yes" && <p role="status" className="muted">Copied to clipboard.</p>}
    {copied === "failed" && <p role="status" className="error">Clipboard unavailable. Select the secret above and copy it manually.</p>}
  </section>;
}

export function SigningHint({webhookUrl}: {webhookUrl: string | null}) {
  return <div className="signing-hint">
    {webhookUrl && <p>Webhook URL: <code>{webhookUrl}</code></p>}
    <p className="muted">Sign every request: send <code>X-OpsPilot-Timestamp</code> (unix seconds) and <code>X-OpsPilot-Signature</code>, the hex HMAC-SHA256 of <code>{"{timestamp}.{raw body}"}</code> keyed with the webhook secret.</p>
  </div>;
}
