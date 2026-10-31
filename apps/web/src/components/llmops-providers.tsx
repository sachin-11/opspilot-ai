"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Async, BreakerBadge, breakerInfo, fmtInt, fmtPct, LlmopsHeader } from "./llmops-common";
import { useOps } from "./providers";

export function LlmopsProviders() {
  const {baseUrl, workspace} = useOps();
  const ws = workspace?.id;
  const health = useQuery({queryKey: ["llmops", "providers", ws], enabled: Boolean(ws), queryFn: () => api.llmopsProviderHealth(baseUrl, ws!), refetchInterval: 30_000});
  return <div className="page-stack llmops">
    <LlmopsHeader title="Providers" intro="Circuit-breaker state and recent error rates for each configured provider and model."/>
    <Async query={health} what="provider health" isEmpty={(rows) => rows.length === 0} empty={<><h3>No providers configured</h3><p>No model configurations are published for this environment.</p></>}>
      {(rows) => <div className="table-wrap panel"><table>
        <caption>Provider and model health</caption>
        <thead><tr><th scope="col">Provider / model</th><th scope="col">Breaker</th><th scope="col">What it means</th><th scope="col">Consecutive failures</th><th scope="col">Error rate</th><th scope="col">Requests (24h)</th><th scope="col">Errors (24h)</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={`${row.provider}/${row.model}`} className={row.breaker_state === "unknown" ? "row-unknown" : undefined}>
          <th scope="row">{row.provider} / {row.model}</th><td><BreakerBadge state={row.breaker_state}/></td>
          <td className="wrap-cell">{breakerInfo(row.breaker_state).meaning}{row.detail && <span className="tag-line">{row.detail}</span>}</td>
          <td>{row.consecutive_failures === null ? "Unknown" : row.consecutive_failures}</td><td>{fmtPct(row.recent_error_rate)}</td><td>{fmtInt(row.requests_24h)}</td><td>{fmtInt(row.errors_24h)}</td>
        </tr>)}</tbody>
      </table></div>}
    </Async>
  </div>;
}
