"use client";

import type { EvalRunDetail, GateReport } from "@/lib/api";
import { fmtMs, fmtPct, fmtUsd, humanize, SyntheticTag } from "./llmops-common";

export function gateSummary(report: GateReport | null) {
  const failed = (report?.results ?? []).filter((result) => result.status === "fail" && !result.overridden);
  return {hardFailed: failed.filter((result) => result.kind === "hard"), softFailed: failed.filter((result) => result.kind === "soft")};
}

const GATE_STATUS = {pass: ["✓", "Pass", "obs-ok"], fail: ["✕", "Fail", "obs-bad"], skipped: ["–", "Skipped", ""]} as const;

export function GateReportView({report}: {report: GateReport | null}) {
  if (!report) return <p className="muted">No gate report: this run has not produced one.</p>;
  return <div className="table-wrap"><table>
    <caption>Quality gates (config {report.config_version})</caption>
    <thead><tr><th scope="col">Gate</th><th scope="col">Type</th><th scope="col">Result</th><th scope="col">Detail</th></tr></thead>
    <tbody>{report.results.map((result) => {
      const [glyph, label, tone] = GATE_STATUS[result.status];
      return <tr key={result.gate}>
        <th scope="row">{humanize(result.gate)}</th>
        <td>{result.kind === "hard" ? "Hard (safety, never overridable)" : "Soft (quality)"}</td>
        <td><span className={`status-badge ${tone}`}><span aria-hidden="true">{glyph} </span>{label}</span>{result.overridden && <span className="status-badge obs-warn"> Overridden</span>}</td>
        <td className="wrap-cell">{result.detail}</td>
      </tr>;
    })}</tbody>
  </table></div>;
}

type MetricRow = {key: string; label: string; kind: "rate" | "ms" | "usd"; higherIsBetter: boolean; measuredOnly?: boolean};
const METRIC_ROWS: MetricRow[] = [
  {key: "pass_rate", label: "Pass rate", kind: "rate", higherIsBetter: true},
  {key: "schema_validity_rate", label: "Schema validity", kind: "rate", higherIsBetter: true},
  {key: "tool_selection_accuracy", label: "Tool selection accuracy", kind: "rate", higherIsBetter: true},
  {key: "citation_validity_rate", label: "Citation validity", kind: "rate", higherIsBetter: true},
  {key: "required_abstention_rate", label: "Required abstention", kind: "rate", higherIsBetter: true},
  {key: "change_correlation_recall", label: "Change correlation recall", kind: "rate", higherIsBetter: true},
  {key: "retrieval_recall_at_k", label: "Retrieval recall@k", kind: "rate", higherIsBetter: true},
  {key: "latency_p50_ms", label: "Latency P50", kind: "ms", higherIsBetter: false, measuredOnly: true},
  {key: "latency_p95_ms", label: "Latency P95", kind: "ms", higherIsBetter: false, measuredOnly: true},
  {key: "avg_cost_usd", label: "Average cost per case", kind: "usd", higherIsBetter: false, measuredOnly: true},
];
const fmt = (kind: MetricRow["kind"], value: number | null | undefined) => value === null || value === undefined ? (kind === "usd" ? fmtUsd(null) : "n/a") : kind === "rate" ? fmtPct(value) : kind === "ms" ? fmtMs(value) : fmtUsd(value);

export function regressedMetrics(run: EvalRunDetail): string[] {
  const control = run.comparison?.control_metrics;
  if (!control) return [];
  return METRIC_ROWS.filter((row) => {
    const a = run.metrics[row.key], b = control[row.key];
    return a !== null && a !== undefined && b !== null && b !== undefined && (row.higherIsBetter ? a < b : a > b);
  }).map((row) => row.label);
}

/** Metrics of a run, side by side with its control run when one was computed. */
export function EvalComparison({run}: {run: EvalRunDetail}) {
  const control = run.comparison;
  const synthetic = run.metrics_source === "synthetic";
  const regressed = new Set(regressedMetrics(run));
  const violationNames = [...new Set([...Object.keys(run.violations), ...Object.keys(control?.control_violations ?? {})])];
  return <div className="page-stack llmops">
    {!control && <p className="muted">No control run was supplied, so there is nothing to compare against.</p>}
    <div className="table-wrap"><table>
      <caption>Metrics{control ? ` compared with control (${control.control_variant})` : ""}</caption>
      <thead><tr><th scope="col">Metric</th><th scope="col">This run</th>{control && <th scope="col">Control</th>}{control && <th scope="col">Result</th>}</tr></thead>
      <tbody>{METRIC_ROWS.map((row) => {
        const value = run.metrics[row.key], base = control?.control_metrics[row.key];
        const isRegression = regressed.has(row.label);
        return <tr key={row.key}>
          <th scope="row">{row.label}{row.measuredOnly && synthetic && <> <SyntheticTag/></>}</th>
          <td>{fmt(row.kind, value)}</td>
          {control && <td>{fmt(row.kind, base)}</td>}
          {control && <td>{isRegression ? <span className="status-badge obs-bad"><span aria-hidden="true">✕ </span>Regressed</span> : value === null || value === undefined || base === null || base === undefined ? "n/a" : <span className="status-badge obs-ok"><span aria-hidden="true">✓ </span>No regression</span>}</td>}
        </tr>;
      })}</tbody>
    </table></div>
    {violationNames.length > 0 && <div className="table-wrap"><table>
      <caption>Safety violations</caption>
      <thead><tr><th scope="col">Invariant</th><th scope="col">This run</th>{control && <th scope="col">Control</th>}</tr></thead>
      <tbody>{violationNames.map((name) => <tr key={name}><th scope="row">{humanize(name)}</th><td>{run.violations[name] ?? 0}{(run.violations[name] ?? 0) > 0 && <span className="status-badge obs-bad"> ✕ violation</span>}</td>{control && <td>{control.control_violations[name] ?? 0}</td>}</tr>)}</tbody>
    </table></div>}
    {control && <section aria-label="Regression list"><h4>Regressions versus control</h4>
      {regressed.size === 0 ? <p className="muted">No metric is worse than the control run.</p> : <ul>{[...regressed].map((name) => <li key={name}>{name} is worse than control.</li>)}</ul>}
    </section>}
  </div>;
}
