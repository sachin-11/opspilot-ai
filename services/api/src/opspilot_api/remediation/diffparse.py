"""Strict unified-diff parser and applier.

The patch is untrusted model output. It is parsed (never passed to ``git apply`` or a shell),
every hunk count is verified, and application requires exact context matches with no fuzz.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

ChangeKind = Literal["modified", "added", "deleted"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_BINARY_MARKERS = ("GIT binary patch", "Binary files ", "literal ", "delta ")
_UNSUPPORTED_HEADERS = (
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
    "dissimilarity index ",
)


class DiffError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # (tag, text) where tag is " ", "+" or "-"; ``no_newline`` flags the following marker.
    lines: list[tuple[str, str]] = field(default_factory=list)
    new_no_newline: bool = False


@dataclass
class FileDiff:
    path: str
    kind: ChangeKind
    hunks: list[Hunk] = field(default_factory=list)
    # Header facts consumed by the policy engine.
    file_mode: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def added(self) -> int:
        return sum(1 for hunk in self.hunks for tag, _ in hunk.lines if tag == "+")

    @property
    def deleted(self) -> int:
        return sum(1 for hunk in self.hunks for tag, _ in hunk.lines if tag == "-")


def normalize_patch(patch_text: str) -> str:
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def patch_hash(patch_text: str) -> str:
    return hashlib.sha256(normalize_patch(patch_text).encode("utf-8")).hexdigest()


def _clean_path(raw: str, side: str) -> str | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        raise DiffError("malformed_diff", "quoted paths are not supported")
    prefix = f"{side}/"
    if not raw.startswith(prefix):
        raise DiffError("malformed_diff", f"path header must start with {prefix}")
    return raw[len(prefix) :]


def parse_unified_diff(patch_text: str) -> list[FileDiff]:
    """Parse a patch into per-file diffs; raise :class:`DiffError` for anything malformed."""
    if "\x00" in patch_text:
        raise DiffError("binary_patch", "patch contains NUL bytes")
    lines = normalize_patch(patch_text).split("\n")[:-1]
    if not lines or not any(line.strip() for line in lines):
        raise DiffError("malformed_diff", "patch is empty")
    files: list[FileDiff] = []
    index = 0
    pending: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            pending = {}
            index += 1
            continue
        if line.startswith(_BINARY_MARKERS):
            raise DiffError("binary_patch", "binary patches are not allowed")
        if line.startswith(_UNSUPPORTED_HEADERS):
            raise DiffError("unsupported_operation", "renames and copies are not allowed")
        if line.startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ")):
            key, _, value = line.rpartition(" ")
            pending[key] = value
            index += 1
            continue
        if line.startswith("index "):
            index += 1
            continue
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise DiffError("malformed_diff", "missing +++ header after ---")
            old_path = _clean_path(line[4:], "a")
            new_path = _clean_path(lines[index + 1][4:], "b")
            if old_path is None and new_path is None:
                raise DiffError("malformed_diff", "both sides are /dev/null")
            if old_path is not None and new_path is not None and old_path != new_path:
                raise DiffError("unsupported_operation", "renames are not allowed")
            kind: ChangeKind = (
                "added" if old_path is None else "deleted" if new_path is None else "modified"
            )
            path = new_path if new_path is not None else old_path
            if path is None:
                raise DiffError("malformed_diff", "missing path")
            file_diff = FileDiff(
                path=path,
                kind=kind,
                file_mode=pending.get("new file mode") or pending.get("deleted file mode"),
                old_mode=pending.get("old mode"),
                new_mode=pending.get("new mode"),
            )
            index += 2
            index = _parse_hunks(lines, index, file_diff)
            if not file_diff.hunks:
                raise DiffError("malformed_diff", f"no hunks for {path}")
            files.append(file_diff)
            pending = {}
            continue
        if line.strip() == "" and not files:
            index += 1
            continue
        raise DiffError("malformed_diff", f"unexpected line outside a hunk: {line[:60]!r}")
    if pending and (pending.get("old mode") or pending.get("new mode")):
        raise DiffError("mode_change", "mode change without content hunks")
    if not files:
        raise DiffError("malformed_diff", "no file diffs found")
    seen: set[str] = set()
    for item in files:
        if item.path in seen:
            raise DiffError("malformed_diff", f"duplicate file diff for {item.path}")
        seen.add(item.path)
    return files


def _parse_hunks(lines: list[str], index: int, file_diff: FileDiff) -> int:
    while index < len(lines):
        header = lines[index]
        if header.startswith(("diff --git ", "--- ")) and file_diff.hunks:
            break
        match = _HUNK.match(header)
        if match is None:
            raise DiffError("malformed_diff", f"expected hunk header, got {header[:60]!r}")
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        hunk = Hunk(int(match.group(1)), old_count, int(match.group(3)), new_count)
        index += 1
        seen_old = seen_new = 0
        while seen_old < old_count or seen_new < new_count:
            if index >= len(lines):
                raise DiffError("malformed_diff", "hunk ends before its declared line counts")
            body = lines[index]
            tag, text = (body[:1], body[1:]) if body else (" ", "")
            if tag == " ":
                seen_old += 1
                seen_new += 1
            elif tag == "-":
                seen_old += 1
            elif tag == "+":
                seen_new += 1
            elif tag == "\\":
                raise DiffError("malformed_diff", "unexpected no-newline marker")
            else:
                raise DiffError("malformed_diff", f"invalid hunk line: {body[:60]!r}")
            hunk.lines.append((tag, text))
            index += 1
            if index < len(lines) and lines[index].startswith("\\"):
                if tag in {" ", "+"}:
                    hunk.new_no_newline = True
                index += 1
        if seen_old != old_count or seen_new != new_count:
            raise DiffError("malformed_diff", "hunk line counts do not match its header")
        file_diff.hunks.append(hunk)
        if index < len(lines) and lines[index].startswith("\\"):
            index += 1
    return index


def _split(content: str) -> tuple[list[str], bool]:
    if content == "":
        return [], True
    has_newline = content.endswith("\n")
    body = content[:-1] if has_newline else content
    return body.split("\n"), has_newline


def apply_file_diff(file_diff: FileDiff, original: str | None) -> str | None:
    """Return new contents (``None`` for a deleted file) or raise on any context mismatch."""
    if file_diff.kind == "added":
        if original is not None:
            raise DiffError("apply_failed", f"{file_diff.path} already exists")
        source: list[str] = []
        had_newline = True
    else:
        if original is None:
            raise DiffError("apply_failed", f"{file_diff.path} does not exist")
        source, had_newline = _split(original)
    output: list[str] = []
    cursor = 0
    new_has_newline = had_newline
    for hunk in file_diff.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if start < cursor or start > len(source):
            raise DiffError("apply_failed", f"hunk out of order or range in {file_diff.path}")
        output.extend(source[cursor:start])
        position = start
        for tag, text in hunk.lines:
            if tag in {" ", "-"}:
                if position >= len(source) or source[position] != text:
                    raise DiffError("apply_failed", f"context mismatch in {file_diff.path}")
                position += 1
                if tag == " ":
                    output.append(text)
            else:
                output.append(text)
        cursor = position
        if cursor >= len(source):
            new_has_newline = not hunk.new_no_newline
    output.extend(source[cursor:])
    if file_diff.kind == "deleted":
        if output:
            raise DiffError("apply_failed", f"deletion of {file_diff.path} leaves content")
        return None
    if file_diff.kind == "added":
        new_has_newline = not file_diff.hunks[-1].new_no_newline
    if not output:
        return ""
    return "\n".join(output) + ("\n" if new_has_newline else "")


def apply_patch(diffs: list[FileDiff], read: Callable[[str], str | None]) -> dict[str, str | None]:
    """Apply every file diff against ``read`` (path -> text or None); nothing is written here."""
    return {item.path: apply_file_diff(item, read(item.path)) for item in diffs}
