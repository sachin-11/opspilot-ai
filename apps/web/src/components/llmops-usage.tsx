"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Budget, type BudgetPatch, type UsageBucket } from "@/lib/api";
import { can } from "@/lib/permissions";
import { Async, BarList, BudgetBar, fmtDate, fmtInt, fmtMs, fmtPct, fmtUsd, LlmopsHeader, Metric, Notice } from "./llmops-common";
import { useOps } from "./providers";

type Filters = {days: string; workflow: string; model: string; provider: string; groupBy: string};
const DEFAULTS: Filters = {days: "30", workflow: "", model: "", provider: "", groupBy: "workflow"};

export function costQuery(filters: Filters, withGroup: boolean): URLSearchParams {
  const query = new URLSearchParams({days: filters.days});
  if (withGroup) query.set("group_by", filters.groupBy);
  for (const key of ["workflow", "model", "provider"] as const) if (filters[key].trim()) query.set(key, filters[key].trim());
  return query;
}

const bucketCost = (bucket: UsageBucket) => bucket.cost_unavailable_requests > 0 && bucket.cost_usd === 0 ? fmtUsd(null) : fmtUsd(bucket.cost_usd);

function BucketTable({title, rows}: {title: string; rows: UsageBucket[]}) {
  if (rows.length === 0) return <section className="panel"><h4>{title}</h4><p className="muted">No spend recorded for this selection.</p></section>;
  return <section className="panel"><div className="table-wrap"><table>
    <caption>{title}</caption>
    <thead><tr><th scope="col">Name</th><th scope="col">Requests</th><th scope="col">Tokens</th><th scope="col">Cost</th></tr></thead>
    <tbody>{rows.map((row) => <tr key={row.key}><th scope="row">{row.key}</th><td>{fmtInt(row.requests)}</td><td>{fmtInt(row.input_tokens + row.output_tokens)}</td>
      <td>{bucketCost(row)}{row.cost_unavailable_requests > 0 && <span className="muted"> ({row.cost_unavailable_requests} request(s) unpriced)</span>}</td></tr>)}</tbody>
  </table></div></section>;
}

export function LlmopsUsage() {
  const {baseUrl, workspace, permissions} = useOps();
  const ws = workspace?.id;
  const [draft, setDraft] = useState<Filters>(DEFAULTS);
  const [applied, setApplied] = useState<Filters>(DEFAULTS);
  const enabled = Boolean(ws);
  const costs = useQuery({queryKey: ["llmops", "costs", ws, costQuery(applied, false).toString()], enabled, queryFn: () => api.llmopsCosts(baseUrl, ws!, costQuery(applied, false))});
  const usage = useQuery({queryKey: ["llmops", "usage", ws, costQuery(applied, true).toString()], enabled, queryFn: () => api.llmopsUsage(baseUrl, ws!, costQuery(applied, true))});
  const budgets = useQuery({queryKey: ["llmops", "budgets", ws], enabled, queryFn: () => api.llmopsBudgets(baseUrl, ws!)});
  const set = (key: keyof Filters) => (event: {target: {value: string}}) => setDraft({...draft, [key]: event.target.value});
  return <div className="page-stack llmops">
    <LlmopsHeader title="Usage and cost" intro="Token usage and spend by workflow, model, provider, and user. Costs are estimates from configured token prices."/>
    <form className="filters" aria-label="Usage filters" onSubmit={(event) => { event.preventDefault(); setApplied(draft); }}>
      <label>Days<select value={draft.days} onChange={set("days")}>{["1", "7", "30", "90"].map((d) => <option key={d} value={d}>Last {d} day(s)</option>)}</select></label>
      <label>Workflow<input value={draft.workflow} onChange={set("workflow")} placeholder="e.g. investigation"/></label>
      <label>Model<input value={draft.model} onChange={set("model")}/></label>
      <label>Provider<input value={draft.provider} onChange={set("provider")}/></label>
      <label>Group usage by<select value={draft.groupBy} onChange={set("groupBy")}>{["workflow", "model", "provider", "node", "user"].map((g) => <option key={g}>{g}</option>)}</select></label>
      <button type="submit" className="button primary">Apply filters</button>
    </form>

    <Async query={costs} what="costs">{(data) => {
      const monthly = new Map<string, number>();
      for (const day of data.daily) monthly.set(day.day.slice(0, 7), (monthly.get(day.day.slice(0, 7)) ?? 0) + day.cost_usd);
      const allUnpriced = data.unavailable_cost_requests > 0 && data.total_cost_usd === 0;
      return <>
        <div className="metric-grid llmops-grid">
          <Metric label="Total estimated cost" value={allUnpriced ? fmtUsd(null) : fmtUsd(data.total_cost_usd)} note={data.unavailable_cost_requests > 0 ? `Cost unavailable for ${data.unavailable_cost_requests} request(s): excluded from the total` : undefined}/>
          <Metric label="Cost per investigation" value={fmtUsd(data.cost_per_investigation_usd)}/>
          <Metric label="Cost per completed remediation" value={fmtUsd(data.cost_per_completed_remediation_usd)}/>
          <Metric label="Reserved vs actual" value={`${fmtUsd(data.reserved_vs_actual.reserved_usd ?? null)} / ${fmtUsd(data.reserved_vs_actual.actual_usd ?? null)}`} note="Reservations are released or settled to actual spend"/>
        </div>
        {data.daily.length === 0 ? <section className="panel empty"><p>No spend recorded in this window. No chart is drawn without data.</p></section> : <section className="panel page-stack">
          <BarList caption="Daily spend" items={data.daily.map((d) => ({label: d.cost_unavailable_requests > 0 ? `${d.day} (${d.cost_unavailable_requests} unpriced)` : d.day, value: d.cost_usd}))} format={(v) => fmtUsd(v)}/>
          <BarList caption="Monthly spend" items={[...monthly].map(([label, value]) => ({label, value}))} format={(v) => fmtUsd(v)}/>
        </section>}
        <BucketTable title="Spend by workflow" rows={data.by_workflow}/>
        <BucketTable title="Spend by model" rows={data.by_model}/>
        <BucketTable title="Spend by provider" rows={data.by_provider}/>
        <BucketTable title="Spend by user" rows={data.by_user}/>
        <section className="panel"><h3>Budget limit events</h3>{data.limit_events.length === 0 ? <p className="muted">No soft or hard limit events in this window.</p> : <div className="table-wrap"><table>
          <caption>Soft and hard limit events</caption><thead><tr><th scope="col">When</th><th scope="col">Event</th><th scope="col">Scope</th><th scope="col">Detail</th></tr></thead>
          <tbody>{data.limit_events.map((event, index) => <tr key={index}><td>{typeof event.at === "string" ? fmtDate(event.at) : "—"}</td><th scope="row">{String(event.kind ?? "")}</th><td>{String(event.scope_key ?? "")}</td><td className="wrap-cell">{String(event.detail ?? "")}</td></tr>)}</tbody></table></div>}</section>
      </>;
    }}</Async>

    <Async query={usage} what="usage">{(data) => <section className="panel page-stack"><h3>Usage</h3>
      <div className="metric-grid llmops-grid">
        <Metric label="Requests" value={fmtInt(data.total_requests)}/><Metric label="Tokens" value={fmtInt(data.total_input_tokens + data.total_output_tokens)}/>
        <Metric label="Latency P50 / P95" value={`${fmtMs(data.latency_p50_ms)} / ${fmtMs(data.latency_p95_ms)}`}/><Metric label="Fallback rate" value={fmtPct(data.fallback_rate)}/><Metric label="Error rate" value={fmtPct(data.error_rate)}/>
      </div>
      <BucketTable title={`Usage by ${data.group_by}`} rows={data.buckets}/>
    </section>}</Async>

    <section className="panel page-stack" aria-labelledby="budgets-heading"><h3 id="budgets-heading">Budgets</h3>
      <Async query={budgets} what="budgets" isEmpty={(rows) => rows.length === 0} empty={<p>No budgets configured.</p>}>
        {(rows) => <>{rows.map((budget) => <div key={budget.id}><BudgetBar budget={budget}/>{can({permissions}, "llmops.budgets.manage") && <BudgetEditor budget={budget}/>}</div>)}</>}
      </Async>
    </section>
  </div>;
}

function BudgetEditor({budget}: {budget: Budget}) {
  const {baseUrl, workspace} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(String(budget.hard_limit_usd));
  const [soft, setSoft] = useState(String(budget.soft_fraction));
  const [enabled, setEnabled] = useState(budget.enabled);
  const [reason, setReason] = useState("");
  const save = useMutation({
    mutationFn: (data: BudgetPatch) => api.llmopsPatchBudget(baseUrl, ws, budget.id, data),
    onSuccess: async () => { setOpen(false); setReason(""); await queryClient.invalidateQueries({queryKey: ["llmops", "budgets"]}); },
  });
  const nextLimit = Number(limit), nextSoft = Number(soft);
  const needsReason = nextLimit > budget.hard_limit_usd || nextSoft > budget.soft_fraction || (budget.enabled && !enabled);
  const validNumbers = nextLimit > 0 && nextLimit <= 1_000_000 && nextSoft > 0 && nextSoft <= 1;
  const valid = validNumbers && (!needsReason || reason.trim().length >= 3);
  if (!open) return <button type="button" className="button" onClick={() => setOpen(true)}>Edit budget for {budget.workflow === "*" ? "all workflows" : budget.workflow}</button>;
  return <form className="form budget-form" aria-label={`Edit budget ${budget.workflow}`} onSubmit={(event) => {
    event.preventDefault();
    save.mutate({hard_limit_usd: nextLimit, soft_fraction: nextSoft, enabled, ...(reason.trim() ? {reason: reason.trim()} : {})});
  }}>
    <div className="form-grid">
      <label>Hard limit (USD)<input type="number" min="0.01" step="any" value={limit} onChange={(event) => setLimit(event.target.value)}/></label>
      <label>Soft limit fraction (0 to 1)<input type="number" min="0.01" max="1" step="any" value={soft} onChange={(event) => setSoft(event.target.value)}/></label>
    </div>
    <label className="check-row"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)}/> Enforce this budget</label>
    <label>Reason{needsReason ? " (required: raising a limit or disabling enforcement)" : " (optional)"}<input value={reason} maxLength={300} onChange={(event) => setReason(event.target.value)}/></label>
    <div className="review-actions"><button type="submit" className="button primary" disabled={!valid || save.isPending}>{save.isPending ? "Saving…" : "Save budget"}</button><button type="button" className="button" onClick={() => setOpen(false)}>Cancel</button></div>
    <Notice error={save.error}/>
  </form>;
}
