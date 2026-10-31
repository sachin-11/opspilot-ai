"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";
import { ApiError, type Budget, type ProviderHealth } from "@/lib/api";

const SECTIONS = [["/llmops", "Overview"], ["/llmops/prompts", "Prompts"], ["/llmops/evaluations", "Evaluations"], ["/llmops/experiments", "Experiments"], ["/llmops/usage", "Usage and cost"], ["/llmops/providers", "Providers"]] as const;

export function LlmopsHeader({title, intro}: {title: string; intro: string}) {
  const path = usePathname() ?? "";
  return <header className="page-header llmops-header">
    <div>
      <p className="eyebrow">LLMOps</p><h2>{title}</h2><p className="muted">{intro}</p>
      <nav className="llmops-tabs" aria-label="LLMOps sections">{SECTIONS.map(([href, label]) => <Link key={href} href={href} aria-current={path === href || (href !== "/llmops" && path.startsWith(`${href}/`)) ? "page" : undefined}>{label}</Link>)}</nav>
    </div>
  </header>;
}

export function apiErrorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.code ? `${error.code}: ` : ""}${error.message}`;
  return error instanceof Error ? error.message : "Unexpected error";
}

export function ErrorPanel({error, what}: {error: unknown; what: string}) {
  return <section className="panel error" role="alert"><h3>{what[0]?.toUpperCase()}{what.slice(1)} could not be loaded</h3><p>{apiErrorText(error)}</p></section>;
}

export function Notice({error, ok}: {error?: unknown; ok?: string | null}) {
  if (error) return <p className="error" role="alert">{apiErrorText(error)}</p>;
  return ok ? <p className="notice" role="status">{ok}</p> : <p className="sr-only" role="status" aria-live="polite"/>;
}

/** Uniform loading / error / empty handling around a query. */
export function Async<T>({query, what, isEmpty, empty, children}: {query: UseQueryResult<T>; what: string; isEmpty?: (data: T) => boolean; empty?: React.ReactNode; children: (data: T) => React.ReactNode}) {
  if (query.isLoading) return <section className="panel" role="status" aria-live="polite">Loading {what}…</section>;
  if (query.isError) return <ErrorPanel error={query.error} what={what}/>;
  if (query.data === undefined) return null;
  if (isEmpty?.(query.data)) return <section className="panel empty" aria-live="polite">{empty ?? <p>No {what} yet.</p>}</section>;
  return <>{children(query.data)}</>;
}

export const fmtInt = (value: number | null) => value === null ? "—" : Math.round(value).toLocaleString("en-US");
export const fmtPct = (value: number | null) => value === null ? "—" : `${(value * 100).toFixed(1)}%`;
export const fmtMs = (value: number | null) => value === null ? "—" : value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
export const fmtUsd = (value: number | null) => value === null ? "Cost unavailable" : `$${value.toFixed(value !== 0 && Math.abs(value) < 1 ? 4 : 2)}`;
export const fmtDate = (value: string | null) => value ? new Date(value).toLocaleString("en-US", {dateStyle: "medium", timeStyle: "short"}) : "—";
export const humanize = (value: string) => value.replaceAll("_", " ");

export const SYNTHETIC = "synthetic (not measured)";
export function SyntheticTag() { return <span className="status-badge obs-warn synthetic-tag" title="Produced by the deterministic reference agent, not by a real model call">{SYNTHETIC}</span>; }

export function Metric({label, value, note, synthetic}: {label: string; value: React.ReactNode; note?: React.ReactNode; synthetic?: boolean}) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong>{synthetic && <SyntheticTag/>}{note && <small className="muted">{note}</small>}</div>;
}

/** Horizontal labelled bars. Values are always printed, so colour is never the only signal. */
export function BarList({items, caption, format = (v: number) => String(v)}: {items: Array<{label: string; value: number}>; caption: string; format?: (value: number) => string}) {
  const max = Math.max(...items.map((item) => item.value), 0);
  return <figure className="bar-list"><figcaption>{caption}</figcaption>
    <ul className="plain-list">{items.map((item) => <li key={item.label} className="bar-row">
      <span className="bar-label">{item.label}</span>
      <span className="bar-track" aria-hidden="true"><span className="bar-fill" style={{width: `${max > 0 ? Math.max(2, (item.value / max) * 100) : 0}%`}}/></span>
      <span className="bar-value">{format(item.value)}</span>
    </li>)}</ul></figure>;
}

/** Line chart of 0..1 rates over time. Only rendered when there are points; includes a table equivalent. */
export function TrendChart({points, caption}: {points: Array<{label: string; value: number}>; caption: string}) {
  const w = 600, h = 180, pad = 28;
  const x = (i: number) => points.length === 1 ? w / 2 : pad + (i * (w - pad * 2)) / (points.length - 1);
  const y = (v: number) => h - pad - v * (h - pad * 2);
  return <figure className="trend-chart" data-testid="trend-chart"><figcaption>{caption}</figcaption>
    <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`${caption}: ${points.map((p) => `${p.label} ${fmtPct(p.value)}`).join(", ")}`}>
      {[0, 0.5, 1].map((tick) => <g key={tick}><line x1={pad} x2={w - pad} y1={y(tick)} y2={y(tick)} className="trend-grid"/><text x={2} y={y(tick) + 4} className="trend-axis">{tick * 100}%</text></g>)}
      <polyline fill="none" className="trend-line" points={points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")}/>
      {points.map((p, i) => <circle key={`${p.label}-${i}`} cx={x(i)} cy={y(p.value)} r={4} className="trend-dot"/>)}
    </svg>
    <div className="table-wrap"><table className="compact"><caption className="sr-only">{caption} (data)</caption><thead><tr><th scope="col">Run</th><th scope="col">Pass rate</th></tr></thead><tbody>{points.map((p, i) => <tr key={`${p.label}-${i}`}><td>{p.label}</td><td>{fmtPct(p.value)}</td></tr>)}</tbody></table></div>
  </figure>;
}

export function ConfirmDialog({title, children, confirmLabel, busy, onConfirm, onCancel}: {title: string; children: React.ReactNode; confirmLabel: string; busy: boolean; onConfirm: () => void; onCancel: () => void}) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { cancel.current?.focus(); }, []);
  return <div className="dialog-backdrop"><div className="dialog panel" role="dialog" aria-modal="true" aria-labelledby="confirm-title" onKeyDown={(event) => { if (event.key === "Escape") onCancel(); }}>
    <h3 id="confirm-title">{title}</h3>
    <div className="dialog-body">{children}</div>
    <div className="review-actions"><button type="button" ref={cancel} className="button" onClick={onCancel} disabled={busy}>Cancel</button><button type="button" className="button primary" onClick={onConfirm} disabled={busy}>{busy ? "Working…" : confirmLabel}</button></div>
  </div></div>;
}

const BREAKER: Record<ProviderHealth["breaker_state"], {label: string; glyph: string; tone: string; meaning: string}> = {
  closed: {label: "Closed", glyph: "✓", tone: "obs-ok", meaning: "Healthy: requests flow normally."},
  open: {label: "Open", glyph: "✕", tone: "obs-bad", meaning: "Tripped after repeated failures: requests are blocked and fall back to another provider if one is configured."},
  half_open: {label: "Half open", glyph: "!", tone: "obs-warn", meaning: "Recovering: a limited number of trial requests are being allowed through."},
  unknown: {label: "Unknown", glyph: "?", tone: "breaker-unknown", meaning: "State could not be read (for example the breaker store is unreachable). Do not assume the provider is healthy."},
};
export const breakerInfo = (state: ProviderHealth["breaker_state"]) => BREAKER[state];
export function BreakerBadge({state}: {state: ProviderHealth["breaker_state"]}) {
  const info = BREAKER[state];
  return <span className={`status-badge ${info.tone}`}><span aria-hidden="true">{info.glyph} </span>{info.label}</span>;
}

const BUDGET_STATE: Record<Budget["state"], {label: string; glyph: string; tone: string}> = {
  ok: {label: "Within budget", glyph: "✓", tone: "obs-ok"},
  soft_warning: {label: "Soft limit reached", glyph: "!", tone: "obs-warn"},
  hard_limit: {label: "Hard limit reached: LLM calls blocked", glyph: "✕", tone: "obs-bad"},
};
export function BudgetBar({budget}: {budget: Budget}) {
  const used = budget.spent_usd + budget.reserved_usd;
  const state = BUDGET_STATE[budget.state];
  const pct = budget.hard_limit_usd > 0 ? Math.min(100, (used / budget.hard_limit_usd) * 100) : 0;
  return <div className={`budget-bar budget-${budget.state}`}>
    <div className="section-heading"><strong>{budget.workflow === "*" ? "All workflows" : budget.workflow} <span className="muted">({budget.scope})</span></strong>
      <span className={`status-badge ${state.tone}`}><span aria-hidden="true">{state.glyph} </span>{state.label}</span></div>
    <progress max={budget.hard_limit_usd} value={Math.min(used, budget.hard_limit_usd)} aria-label={`Budget used for ${budget.workflow}`}>{pct.toFixed(0)}%</progress>
    <p className="muted">${budget.spent_usd.toFixed(4)} spent + ${budget.reserved_usd.toFixed(4)} reserved of ${budget.hard_limit_usd.toFixed(2)} hard limit ({pct.toFixed(0)}%). Soft limit ${budget.soft_threshold_usd.toFixed(2)}.{!budget.enabled && " Enforcement is disabled."}{budget.unmetered_calls > 0 && ` ${budget.unmetered_calls} call(s) could not be metered.`}</p>
  </div>;
}
