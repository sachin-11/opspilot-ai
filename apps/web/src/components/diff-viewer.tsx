"use client";

import { useState } from "react";

export type DiffLine = {kind: "add" | "del" | "ctx"; oldNo: number | null; newNo: number | null; text: string};
export type DiffFile = {path: string; change: "modified" | "added" | "deleted"; lines: DiffLine[]; added: number; deleted: number; raw: string[]};
export type BlockedFile = {path: string; reasons: string[]};

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
const CONTEXT_KEEP = 3;

export function parseDiff(patch: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let oldNo = 0;
  let newNo = 0;
  let pendingOld: string | null = null;
  for (const line of patch.replaceAll("\r\n", "\n").split("\n")) {
    if (line.startsWith("--- ")) { pendingOld = line.slice(4).replace(/^a\//, ""); continue; }
    if (line.startsWith("+++ ") && pendingOld !== null) {
      const target = line.slice(4).replace(/^b\//, "");
      const deleted = target === "/dev/null";
      const added = pendingOld === "/dev/null";
      current = {path: deleted ? pendingOld : target, change: deleted ? "deleted" : added ? "added" : "modified", lines: [], added: 0, deleted: 0, raw: []};
      files.push(current);
      pendingOld = null;
      continue;
    }
    if (!current) continue;
    const hunk = HUNK.exec(line);
    if (hunk) { oldNo = Number(hunk[1]); newNo = Number(hunk[2]); current.raw.push(line); continue; }
    if (line.startsWith("+")) { current.lines.push({kind: "add", oldNo: null, newNo: newNo++, text: line.slice(1)}); current.added += 1; current.raw.push(line); }
    else if (line.startsWith("-")) { current.lines.push({kind: "del", oldNo: oldNo++, newNo: null, text: line.slice(1)}); current.deleted += 1; current.raw.push(line); }
    else if (line.startsWith(" ")) { current.lines.push({kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: line.slice(1)}); current.raw.push(line); }
  }
  return files;
}

type Row = {type: "line"; line: DiffLine; key: string} | {type: "collapsed"; lines: DiffLine[]; key: string};

function toRows(lines: DiffLine[], expanded: Set<string>): Row[] {
  const rows: Row[] = [];
  let index = 0;
  while (index < lines.length) {
    const first = lines[index]!;
    if (first.kind !== "ctx") { rows.push({type: "line", line: first, key: `l${index}`}); index += 1; continue; }
    let end = index;
    while (end < lines.length && lines[end]!.kind === "ctx") end += 1;
    const run = lines.slice(index, end);
    const leading = index === 0 ? 0 : CONTEXT_KEEP;
    const trailing = end === lines.length ? 0 : CONTEXT_KEEP;
    const key = `c${index}`;
    if (run.length > leading + trailing + 1 && !expanded.has(key)) {
      run.slice(0, leading).forEach((line, offset) => rows.push({type: "line", line, key: `${key}-a${offset}`}));
      rows.push({type: "collapsed", lines: run.slice(leading, run.length - trailing), key});
      run.slice(run.length - trailing).forEach((line, offset) => rows.push({type: "line", line, key: `${key}-b${offset}`}));
    } else {
      run.forEach((line, offset) => rows.push({type: "line", line, key: `${key}-${offset}`}));
    }
    index = end;
  }
  return rows;
}

function Text({text}: {text: string}) {
  const parts = text.split("[REDACTED]");
  return <>{parts.map((part, index) => <span key={index}>{part}{index < parts.length - 1 && <mark className="diff-redacted" title="Secret redacted">[REDACTED]</mark>}</span>)}</>;
}

function FileDiffView({file, blocked}: {file: DiffFile; blocked?: BlockedFile}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const rows = toRows(file.lines, expanded);
  return <details className={`diff-file${blocked ? " blocked" : ""}`} open>
    <summary><code>{file.path}</code> <span className="diff-count add">+{file.added}</span> <span className="diff-count del">−{file.deleted}</span> <span className="muted">{file.change}</span>{blocked && <span className="status-badge diff-blocked">Blocked file</span>}</summary>
    {blocked && <p role="alert" className="error diff-blocked-note">Policy blocks changes to this file: {blocked.reasons.join(", ")}.</p>}
    <div className="diff-scroll"><table className="diff-table" aria-label={`Diff of ${file.path}`}><tbody>
      {rows.map((row) => row.type === "collapsed"
        ? <tr key={row.key} className="diff-collapsed"><td colSpan={4}><button type="button" className="link-button" onClick={() => setExpanded(new Set([...expanded, row.key]))}>Show {row.lines.length} unchanged lines</button></td></tr>
        : <tr key={row.key} className={`diff-${row.line.kind}`}><td className="diff-no" aria-hidden="true">{row.line.oldNo ?? ""}</td><td className="diff-no" aria-hidden="true">{row.line.newNo ?? ""}</td><td className="diff-mark" aria-hidden="true">{row.line.kind === "add" ? "+" : row.line.kind === "del" ? "−" : " "}</td><td className="diff-text"><Text text={row.line.text || " "}/></td></tr>)}
    </tbody></table></div>
    <pre className="diff-mobile" aria-label={`Plain diff of ${file.path}`}><Text text={file.raw.join("\n")}/></pre>
  </details>;
}

export function DiffViewer({patch, blocked = []}: {patch: string; blocked?: BlockedFile[]}) {
  const files = parseDiff(patch);
  if (!files.length) return <p className="muted" role="status">No parsable diff is available for this revision.</p>;
  const byPath = new Map(blocked.map((item) => [item.path, item]));
  return <div className="diff-viewer" role="group" aria-label="Unified diff (read-only)">
    {files.map((file) => <FileDiffView key={file.path} file={file} blocked={byPath.get(file.path)}/>)}
    <p className="muted diff-note">Read-only view. Patches cannot be edited here; request a revision instead.</p>
  </div>;
}
