"""Bounded text, Markdown, and text-PDF validation and extraction."""

import io
import re
import time
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

from opspilot_api.config import Settings
from opspilot_api.incidents.service import ValidationError
from opspilot_api.knowledge.schemas import ExtractedBlock, ExtractedDocument

SUPPORTED_MIME_TYPES = {"text/plain", "text/markdown", "application/pdf"}
TEXT_EXTENSIONS = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown"}


class DocumentValidationError(ValidationError):
    code = "invalid_document"


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    result: list[str] = []
    empty = False
    for line in lines:
        if line:
            result.append(line)
            empty = False
        elif result and not empty:
            result.append("")
            empty = True
    return "\n".join(result).strip()


def validate_and_detect(
    content: bytes, declared_mime: str | None, filename: str | None, settings: Settings
) -> str:
    if not content:
        raise DocumentValidationError("document is empty")
    if len(content) > settings.KNOWLEDGE_MAX_FILE_BYTES:
        raise DocumentValidationError("document exceeds configured size limit")
    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    extension = Path(filename or "").suffix.lower()
    if content.startswith(b"%PDF-"):
        detected = "application/pdf"
    else:
        if content.startswith((b"MZ", b"PK\x03\x04", b"\x7fELF")) or b"\x00" in content[:4096]:
            raise DocumentValidationError("unsupported binary document")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("text documents must be UTF-8") from exc
        detected = TEXT_EXTENSIONS.get(extension, "text/plain")
    if mime and mime not in SUPPORTED_MIME_TYPES:
        raise DocumentValidationError("unsupported declared MIME type")
    if mime and mime != detected and not ({mime, detected} <= {"text/plain", "text/markdown"}):
        raise DocumentValidationError("declared MIME type does not match file signature")
    return detected


def _plain_blocks(text: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    offset = 0
    for paragraph in re.split(r"\n\s*\n", text):
        normalized = normalize_text(paragraph)
        if not normalized:
            continue
        start = text.find(paragraph, offset)
        end = start + len(paragraph)
        blocks.append(ExtractedBlock(content=normalized, start_offset=start, end_offset=end))
        offset = end
    return blocks


def _markdown_blocks(text: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    headings: list[str] = []
    current: list[str] = []
    block_start = 0
    in_code = False

    def flush(end: int) -> None:
        nonlocal current, block_start
        content = normalize_text("\n".join(current))
        if content:
            blocks.append(
                ExtractedBlock(
                    content=content,
                    heading_path=list(headings),
                    start_offset=block_start,
                    end_offset=end,
                )
            )
        current = []

    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line) if not in_code else None
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if heading:
            flush(offset)
            level = len(heading.group(1))
            headings[:] = headings[: level - 1]
            headings.append(normalize_text(heading.group(2)))
            block_start = offset + len(raw_line)
        elif not line.strip() and not in_code:
            flush(offset)
            block_start = offset + len(raw_line)
        else:
            if not current:
                block_start = offset
            current.append(line)
        offset += len(raw_line)
    flush(len(text))
    return blocks


def extract_document(content: bytes, mime_type: str, settings: Settings) -> ExtractedDocument:
    started = time.monotonic()
    if mime_type in {"text/plain", "text/markdown"}:
        text = normalize_text(content.decode("utf-8"))
        blocks = _markdown_blocks(text) if mime_type == "text/markdown" else _plain_blocks(text)
        result = ExtractedDocument(blocks=blocks)
    elif mime_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentValidationError("encrypted PDFs are not supported")
            if len(reader.pages) > settings.KNOWLEDGE_MAX_PDF_PAGES:
                raise DocumentValidationError("PDF exceeds configured page limit")
            blocks = []
            for page_number, page in enumerate(reader.pages, start=1):
                extracted = normalize_text(page.extract_text() or "")
                if extracted:
                    blocks.extend(
                        ExtractedBlock(content=block.content, page_number=page_number)
                        for block in _plain_blocks(extracted)
                    )
            result = ExtractedDocument(blocks=blocks, page_count=len(reader.pages))
        except DocumentValidationError:
            raise
        except (PdfReadError, FileNotDecryptedError) as exc:
            raise DocumentValidationError("invalid or unreadable PDF") from exc
    else:
        raise DocumentValidationError("unsupported document type")
    character_count = sum(len(block.content) for block in result.blocks)
    if character_count < 20:
        raise DocumentValidationError("document has no meaningful extractable text")
    if character_count > settings.KNOWLEDGE_MAX_EXTRACTED_CHARS:
        raise DocumentValidationError("extracted text exceeds configured character limit")
    if time.monotonic() - started > settings.KNOWLEDGE_EXTRACTION_TIMEOUT_SECONDS:
        raise DocumentValidationError("document extraction exceeded configured time limit")
    return result
