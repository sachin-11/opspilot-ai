"""Deterministic structure-aware chunking with bounded overlap."""

import hashlib
import re

from opspilot_api.config import Settings
from opspilot_api.investigations.safety import redact_text
from opspilot_api.knowledge.schemas import ChunkDraft, ExtractedDocument

TOKEN_PATTERN = re.compile(r"\S+")


def token_count(text: str) -> int:
    return len(TOKEN_PATTERN.findall(text))


def _token_windows(text: str, target: int, overlap: int, maximum: int) -> list[str]:
    tokens = TOKEN_PATTERN.findall(text)
    if len(tokens) <= maximum:
        return [" ".join(tokens)]
    size = min(target, maximum)
    step = max(1, size - overlap)
    windows: list[str] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + size]
        if not window:
            break
        windows.append(" ".join(window))
        if start + size >= len(tokens):
            break
    return windows


def chunk_document(document: ExtractedDocument, settings: Settings) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    pending = ""
    pending_heading: list[str] = []
    pending_page: int | None = None
    pending_start: int | None = None
    pending_end: int | None = None

    def emit() -> None:
        nonlocal pending, pending_heading, pending_page, pending_start, pending_end
        body = pending.strip()
        heading_context = " > ".join(pending_heading)
        searchable_content = f"{heading_context}\n\n{body}" if heading_context else body
        redacted = redact_text(searchable_content)
        if not redacted:
            pending = ""
            return
        for window in _token_windows(
            redacted,
            settings.KNOWLEDGE_CHUNK_TARGET_TOKENS,
            settings.KNOWLEDGE_CHUNK_OVERLAP_TOKENS,
            settings.KNOWLEDGE_CHUNK_MAX_TOKENS,
        ):
            count = token_count(window)
            if count < settings.KNOWLEDGE_CHUNK_MIN_TOKENS and drafts:
                previous = drafts[-1]
                combined = f"{previous.content}\n\n{window}"
                if token_count(combined) <= settings.KNOWLEDGE_CHUNK_MAX_TOKENS:
                    previous.content = combined
                    previous.token_count = token_count(combined)
                    previous.content_hash = hashlib.sha256(combined.encode()).hexdigest()
                    continue
            drafts.append(
                ChunkDraft(
                    chunk_index=len(drafts),
                    content=window,
                    content_hash=hashlib.sha256(window.encode()).hexdigest(),
                    token_count=count,
                    heading_path=list(pending_heading),
                    page_number=pending_page,
                    start_offset=pending_start,
                    end_offset=pending_end,
                )
            )
        pending = ""
        pending_heading = []
        pending_page = None
        pending_start = None
        pending_end = None

    for block in document.blocks:
        block_tokens = token_count(block.content)
        same_structure = block.heading_path == pending_heading and block.page_number == pending_page
        if pending and (
            not same_structure
            or token_count(pending) + block_tokens > settings.KNOWLEDGE_CHUNK_TARGET_TOKENS
        ):
            emit()
        if not pending:
            pending_heading = list(block.heading_path)
            pending_page = block.page_number
            pending_start = block.start_offset
        pending = f"{pending}\n\n{block.content}".strip()
        pending_end = block.end_offset
    emit()
    return drafts
