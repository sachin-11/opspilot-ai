"""Deterministic, language-aware code chunking with exact line ranges.

Input is already-redacted text whose line count equals the original file's, so every chunk's
``start_line``/``end_line`` maps exactly onto the source. No code is executed; Python is parsed
with ``ast`` only and other languages use declaration heuristics, falling back to bounded line
windows. Identical input always yields identical chunks and hashes.
"""

import ast
import hashlib
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"\w+|[^\w\s]")
_TS_DECLARATION = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?"
    r"(?:(?:async\s+)?function\s*\*?\s*(?P<fn>\w+)"
    r"|(?P<kind>class|interface|enum|type)\s+(?P<cls>\w+)"
    r"|(?:const|let|var)\s+(?P<var>\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>)"
)
_GO_DECLARATION = re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?(?P<fn>\w+)|type\s+(?P<ty>\w+))")
_SHELL_FUNCTION = re.compile(r"^(?:function\s+)?(?P<fn>[A-Za-z_][\w-]*)\s*\(\)\s*\{?")
_TERRAFORM_BLOCK = re.compile(
    r'^(?P<kind>resource|module|variable|output|data|provider|locals)\s*(?:"(?P<a>[^"]+)")?\s*(?:"(?P<b>[^"]+)")?'
)
_SQL_STATEMENT = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:unique\s+)?(?P<kind>table|index|view|function|trigger|type)"
    r"\s+(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"]+)",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(?P<name>.+?)\s*$")
_JAVA_RUST = re.compile(
    r"^(?:pub\s+)?(?:public\s+|private\s+|protected\s+|static\s+)*"
    r"(?:(?:class|interface|enum|struct|trait|impl)\s+(?P<ty>\w+)|fn\s+(?P<fn>\w+))"
)


@dataclass(frozen=True)
class CodeChunkDraft:
    path: str
    language: str | None
    symbol_name: str | None
    symbol_type: str | None
    start_line: int
    end_line: int
    content: str
    content_hash: str
    token_count: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class ChunkingResult:
    chunks: list[CodeChunkDraft]
    parse_failed: bool = False


@dataclass(frozen=True)
class _Block:
    start: int  # 1-based inclusive
    end: int
    name: str | None
    kind: str | None


def token_count(text: str) -> int:
    return len(_WORD.findall(text))


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _python_blocks(text: str, total: int, max_lines: int) -> list[_Block] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    blocks: list[_Block] = []

    def visit(
        nodes: list[ast.stmt], prefix: str, container_kind: str | None, top_level: bool
    ) -> None:
        for node in nodes:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            start = node.lineno
            if node.decorator_list:
                start = min(start, *(item.lineno for item in node.decorator_list))
            end = node.end_lineno or node.lineno
            name = f"{prefix}{node.name}"
            if isinstance(node, ast.ClassDef):
                if end - start + 1 > max_lines:
                    # Oversized class: descend so methods become their own chunks.
                    methods_before = len(blocks)
                    visit(node.body, f"{name}.", "class", False)
                    if len(blocks) == methods_before:
                        blocks.append(_Block(start, end, name, "class"))
                    continue
                blocks.append(_Block(start, end, name, "class"))
            else:
                kind = "method" if container_kind == "class" else "function"
                blocks.append(_Block(start, end, name, kind))

    visit(tree.body, "", None, True)
    return blocks


def _marker_blocks(lines: list[str], language: str) -> list[_Block]:
    """Column-0 declaration heuristics; a block ends where the next declaration begins."""
    starts: list[tuple[int, str | None, str | None]] = []
    for index, line in enumerate(lines, start=1):
        if not line or line[0] in " \t":
            continue
        if language in {"typescript", "javascript"}:
            match = _TS_DECLARATION.match(line)
            if match:
                name = match.group("fn") or match.group("cls") or match.group("var")
                kind = match.group("kind") or "function"
                starts.append((index, name, kind))
        elif language == "go":
            match = _GO_DECLARATION.match(line)
            if match:
                starts.append((index, match.group("fn") or match.group("ty"), "function"))
        elif language == "shell":
            match = _SHELL_FUNCTION.match(line)
            if match and "(" in line:
                starts.append((index, match.group("fn"), "function"))
        elif language == "terraform":
            match = _TERRAFORM_BLOCK.match(line)
            if match:
                name = ".".join(item for item in (match.group("a"), match.group("b")) if item)
                starts.append((index, name or match.group("kind"), match.group("kind")))
        elif language in {"java", "rust"}:
            match = _JAVA_RUST.match(line)
            if match:
                starts.append((index, match.group("ty") or match.group("fn"), "class"))
        elif language == "sql":
            match = _SQL_STATEMENT.match(line)
            if match:
                starts.append((index, match.group("name"), match.group("kind").lower()))
        elif language == "markdown":
            match = _MARKDOWN_HEADING.match(line)
            if match:
                starts.append((index, match.group("name"), "section"))
    blocks: list[_Block] = []
    for position, (start, name, kind) in enumerate(starts):
        next_start = starts[position + 1][0] if position + 1 < len(starts) else len(lines) + 1
        end = next_start - 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        blocks.append(_Block(start, end, name, kind))
    return blocks


def _windows(start: int, end: int, max_lines: int, overlap: int) -> list[tuple[int, int]]:
    """Split [start, end] into bounded windows, overlapping only when a split is required."""
    if end - start + 1 <= max_lines:
        return [(start, end)]
    result: list[tuple[int, int]] = []
    cursor = start
    step = max_lines - overlap
    while cursor <= end:
        window_end = min(end, cursor + max_lines - 1)
        result.append((cursor, window_end))
        if window_end == end:
            break
        cursor += step
    return result


def _clean_range(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    return (start, end) if start <= end else None


def chunk_file(
    path: str, language: str | None, text: str, max_lines: int, overlap_lines: int
) -> ChunkingResult:
    """Chunk redacted file text. ``parse_failed`` marks a language parser fallback."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return ChunkingResult([])
    parse_failed = False
    blocks: list[_Block] = []
    if language == "python":
        parsed = _python_blocks("\n".join(lines), total, max_lines)
        if parsed is None:
            parse_failed = True
        else:
            blocks = parsed
    elif (
        language
        in {
            "typescript",
            "javascript",
            "go",
            "shell",
            "terraform",
            "java",
            "rust",
            "sql",
        }
        or language == "markdown"
    ):
        blocks = _marker_blocks(lines, language)

    # Cover every line exactly once: symbol blocks plus module-level gaps between them.
    segments: list[_Block] = []
    cursor = 1
    for block in sorted(blocks, key=lambda item: (item.start, item.end)):
        if block.start < cursor:
            continue  # nested/overlapping declaration already covered by its parent
        if block.start > cursor:
            segments.append(_Block(cursor, block.start - 1, None, "module"))
        segments.append(block)
        cursor = block.end + 1
    if cursor <= total:
        segments.append(_Block(cursor, total, None, "module" if blocks else None))

    chunks: list[CodeChunkDraft] = []
    for segment in segments:
        cleaned = _clean_range(lines, segment.start, segment.end)
        if cleaned is None:
            continue
        start, end = cleaned
        split = _windows(start, end, max_lines, overlap_lines)
        for part, (window_start, window_end) in enumerate(split):
            content = "\n".join(lines[window_start - 1 : window_end])
            chunks.append(
                CodeChunkDraft(
                    path=path,
                    language=language,
                    symbol_name=segment.name,
                    symbol_type=segment.kind,
                    start_line=window_start,
                    end_line=window_end,
                    content=content,
                    content_hash=_digest(content),
                    token_count=max(1, token_count(content)),
                    metadata={"part": part + 1, "parts": len(split)} if len(split) > 1 else {},
                )
            )
    return ChunkingResult(chunks, parse_failed)
