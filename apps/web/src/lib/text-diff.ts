export type DiffKind = "same" | "add" | "del";
export type DiffLine = {kind: DiffKind; text: string; oldNo: number | null; newNo: number | null};

/** Small line diff (longest common subsequence). Inputs are prompt templates, so sizes stay modest. */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
  const table: number[][] = Array.from({length: a.length + 1}, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i]![j] = a[i] === b[j] ? table[i + 1]![j + 1]! + 1 : Math.max(table[i + 1]![j]!, table[i]![j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push({kind: "same", text: a[i]!, oldNo: i + 1, newNo: j + 1});
      i++; j++;
    } else if (i < a.length && (j === b.length || table[i + 1]![j]! >= table[i]![j + 1]!)) {
      out.push({kind: "del", text: a[i]!, oldNo: i + 1, newNo: null});
      i++;
    } else {
      out.push({kind: "add", text: b[j]!, oldNo: null, newNo: j + 1});
      j++;
    }
  }
  return out;
}

export function diffStats(lines: DiffLine[]): {added: number; removed: number} {
  return {added: lines.filter((line) => line.kind === "add").length, removed: lines.filter((line) => line.kind === "del").length};
}
