"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api, type EvalRunSummary } from "@/lib/api";
import { Async, BreakerBadge, BudgetBar, fmtDate, fmtInt, fmtMs, fmtPct, fmtUsd, LlmopsHeader, Metric, SYNTHETIC, TrendChart } from "./llmops-common";
import { useOps } from "./providers";

const passRate = (run: EvalRunSummary) => run.total_cases > 0 ? run.passed_cases / run.total_cases : null;

export function LlmopsOverview() {
  const {baseUrl, workspace} = useOps();
  const ws = workspace?.id;
  const enabled = Boolean(ws);
  const usageQuery = new URLSearchParams({days: "7", group_by: "workflow"});
  const costQuery = new URLSearchParams({days: "7"});
  const runs = useQuery({queryKey: ["llmops", "eval-runs", ws], enabled, queryFn: () => api.llmopsEvalRuns(baseUrl, ws!)});
  const usage = useQuery({queryKey: ["llmops", "usage", ws, usageQuery.toString()], enabled, queryFn: () => api.llmopsUsage(baseUrl, ws!, usageQuery)});
  const costs = useQuery({queryKey: ["llmops", "costs", ws, costQuery.toString()], enabled, queryFn: () => api.llmopsCosts(baseUrl, ws!, costQuery)});
  const budgets = useQuery({queryKey: ["llmops", "budgets", ws], enabled, queryFn: () => api.llmopsBudgets(baseUrl, ws!)});
  const health = useQuery({queryKey: ["llmops", "providers", ws], enabled, queryFn: () => api.llmopsProviderHealth(baseUrl, ws!)});
  const completed = (runs.data ?? []).filter((run) => run.status === "completed" && run.total_cases > 0).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latest = completed.at(-1);
  const latestDetail = useQuery({queryKey: ["llmops", "eval-run", ws, latest?.id], enabled: enabled && Boolean(latest), queryFn: () => api.llmopsEvalRun(baseUrl, ws!, latest!.id)});

  return <div className="page-stack llmops">
    <LlmopsHeader title="LLM quality and cost" intro="Evaluation quality, safety, latency, spend, and provider health for the models behind the agents."/>

    <section className="panel" aria-labelledby="trend-heading"><h3 id="trend-heading">Quality trend</h3>
      <Async query={runs} what="evaluation runs" isEmpty={() => completed.length === 0} empty={<><h3>No completed evaluation runs</h3><p>No chart is drawn without data. Start a run to establish a quality baseline.</p><Link className="button primary" href="/llmops/evaluations">Open evaluations</Link></>}>
        {() => <TrendChart caption="Pass rate per completed evaluation run" points={completed.map((run) => ({label: `${fmtDate(run.created_at)} (${run.variant})`, value: passRate(run) ?? 0}))}/>}
      </Async>
    </section>

    <section aria-labelledby="latest-heading"><h3 id="latest-heading">Latest evaluation run</h3>
      {!latest ? <section className="panel empty"><p>No completed run to summarise.</p></section> : <Async query={latestDetail} what="latest evaluation run">
        {(run) => {
          const synthetic = run.metrics_source === "synthetic";
          const violations = Object.values(run.violations).reduce((sum, count) => sum + count, 0);
          return <div className="metric-grid llmops-grid">
            <Metric label="Schema validity" value={fmtPct(run.metrics.schema_validity_rate ?? null)}/>
            <Metric label="Citation validity" value={fmtPct(run.metrics.citation_validity_rate ?? null)}/>
            <Metric label="Tool accuracy" value={fmtPct(run.metrics.tool_selection_accuracy ?? null)}/>
            <Metric label="Safety violations" value={<span className={violations > 0 ? "error" : undefined}>{violations}{violations > 0 ? " ✕" : ""}</span>} note={violations === 0 ? "None detected" : "Hard gates will block publication"}/>
            <Metric label="Eval tokens" value={fmtInt(run.total_tokens)} synthetic={synthetic}/>
            <Metric label="Eval latency P50 / P95" value={`${fmtMs(run.metrics.latency_p50_ms ?? null)} / ${fmtMs(run.metrics.latency_p95_ms ?? null)}`} synthetic={synthetic}/>
            <p className="muted metric-source">Source: {run.metrics_source === "synthetic" ? SYNTHETIC : "measured"} · <Link href={`/llmops/evaluations/${run.id}`}>open report</Link></p>
          </div>;
        }}
      </Async>}
    </section>

    <section aria-labelledby="live-heading"><h3 id="live-heading">Production usage (last 7 days)</h3>
      <div className="metric-grid llmops-grid">
        <Async query={usage} what="usage">{(data) => <>
          <Metric label="Requests" value={fmtInt(data.total_requests)} note={data.total_requests === 0 ? "No LLM requests in this window" : undefined}/>
          <Metric label="Tokens" value={fmtInt(data.total_input_tokens + data.total_output_tokens)} note={`${fmtInt(data.total_input_tokens)} in / ${fmtInt(data.total_output_tokens)} out`}/>
          <Metric label="Latency P50 / P95" value={`${fmtMs(data.latency_p50_ms)} / ${fmtMs(data.latency_p95_ms)}`}/>
          <Metric label="Fallback rate" value={fmtPct(data.fallback_rate)}/>
          <Metric label="Provider error rate" value={fmtPct(data.error_rate)}/>
        </>}</Async>
        <Async query={costs} what="costs">{(data) => <Metric label="Estimated cost" value={data.unavailable_cost_requests > 0 && data.total_cost_usd === 0 ? fmtUsd(null) : fmtUsd(data.total_cost_usd)} note={data.unavailable_cost_requests > 0 ? `${data.unavailable_cost_requests} request(s) have no pricing and are excluded` : "Estimate from configured token prices"}/>}</Async>
      </div>
    </section>

    <section className="panel" aria-labelledby="breaker-heading"><h3 id="breaker-heading">Circuit breakers</h3>
      <Async query={health} what="provider health" isEmpty={(rows) => rows.length === 0}>
        {(rows) => <ul className="plain-list">{rows.map((row) => <li key={`${row.provider}/${row.model}`} className="section-heading"><span>{row.provider} / {row.model}</span><BreakerBadge state={row.breaker_state}/></li>)}</ul>}
      </Async>
      <p><Link href="/llmops/providers">Provider details</Link></p>
    </section>

    <section className="panel" aria-labelledby="budget-heading"><h3 id="budget-heading">Budget consumption</h3>
      <Async query={budgets} what="budgets" isEmpty={(rows) => rows.length === 0} empty={<p>No budgets configured.</p>}>
        {(rows) => <div className="plain-list">{rows.map((budget) => <BudgetBar key={budget.id} budget={budget}/>)}</div>}
      </Async>
      <p><Link href="/llmops/usage">Usage and cost</Link></p>
    </section>
  </div>;
}
