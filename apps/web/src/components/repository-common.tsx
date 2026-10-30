"use client";

import { isProviderUnavailable, type CodeSearchResult } from "@/lib/api";

const MAX_EXCERPT_LINES = 40;

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

export function QueryFailure({error, what}: {error: unknown; what: string}) {
  if (isProviderUnavailable(error)) {
    return <section className="panel provider-unavailable" role="alert"><h3>Provider unavailable</h3><p>The GitHub provider could not be reached, so {what} cannot be shown right now. No data has been changed. Try again shortly.</p></section>;
  }
  return <section className="panel error" role="alert">{what[0]?.toUpperCase()}{what.slice(1)} could not be loaded.</section>;
}

export function CodeExcerpt({result}: {result: Pick<CodeSearchResult, "excerpt" | "start_line" | "path">}) {
  const lines = result.excerpt.split("\n");
  const shown = lines.slice(0, MAX_EXCERPT_LINES);
  return <div className="code-excerpt" role="group" aria-label={`Excerpt of ${result.path}`}>
    <pre><code>{shown.map((line, index) => <span className="code-line" key={index}><span className="line-no" aria-hidden="true">{result.start_line + index}</span><span className="line-text">{line || " "}</span></span>)}</code></pre>
    {lines.length > shown.length && <small className="muted">Excerpt truncated to {MAX_EXCERPT_LINES} lines.</small>}
  </div>;
}

export function ScoreGrid({result}: {result: CodeSearchResult}) {
  return <div className="score-grid"><span>Lexical <strong>{result.lexical_score?.toFixed(4) ?? "—"}</strong></span><span>Vector <strong>{result.vector_score?.toFixed(4) ?? "—"}</strong></span><span>RRF <strong>{result.rrf_score?.toFixed(4) ?? "—"}</strong></span><span>Rerank <strong>{result.rerank_score?.toFixed(4) ?? "—"}</strong></span></div>;
}

export function CodeResultCard({result, showScores = true}: {result: CodeSearchResult; showScores?: boolean}) {
  return <article className="panel code-result">
    <h3>{result.citation_label}</h3>
    <p className="code-location"><span>{result.repository_full_name}</span> <code>{result.path}:{result.start_line}-{result.end_line}</code> <small className="muted">{shortSha(result.commit_sha)} · {result.branch}</small></p>
    <CodeExcerpt result={result}/>
    {showScores && <ScoreGrid result={result}/>}
  </article>;
}
