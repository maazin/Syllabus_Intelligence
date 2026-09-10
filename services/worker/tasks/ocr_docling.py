"""OCR via Docling — PRD section 18.1.

Isolated in its own module so that importing the ingestion pipeline does not
drag in Docling's model weights. Most documents have a usable text layer and
never reach this code path (~85% per section 7).

Why Docling and not Tesseract: Tesseract extracts text and word boxes but has
no table-structure API, and tables are the deciding requirement (section 7).
Docling runs ~1.5 pages/sec on CPU with no GPU. PaddleOCR/PP-StructureV3 is the
documented alternative; Marker and Surya are excluded because their model
weights are RAIL-M restricted despite Apache-2.0 code.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def ocr_with_docling(data: bytes) -> list:
    """Run Docling over a scanned PDF or image, returning ExtractedPage objects.

    Imported lazily inside the function body: `docling` pulls in torch and model
    weights, and the API process must never pay that import cost.
    """
    from services.worker.tasks.ingest import ExtractedPage, IngestionError

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise IngestionError(
            "OCR_MODE=docling requires the `docling` package. Install the worker's "
            "OCR extras, or set OCR_MODE to an available engine."
        ) from exc

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            converter = DocumentConverter()
            result = converter.convert(Path(tmp.name))
        except Exception as exc:
            raise IngestionError(f"Docling could not process the document: {exc}") from exc

    markdown = result.document.export_to_markdown()
    return _split_markdown_into_pages(markdown, ExtractedPage)


def _split_markdown_into_pages(markdown: str, page_cls: type) -> list:
    """Docling emits one markdown stream; recover page boundaries where it marks them.

    Docling's page markers vary by version, so a document with no recoverable
    markers becomes a single page rather than failing. `page_ref` accuracy
    degrades in that case, which the review screen shows as a page-1 citation —
    imprecise, but never wrong in a way that fabricates a source.
    """
    parts = re.split(r"\n<!--\s*page[ _-]?(?:break|\d+).*?-->\n", markdown, flags=re.IGNORECASE)
    parts = [p for p in parts if p.strip()]
    if not parts:
        return []

    pages = []
    for index, part in enumerate(parts, start=1):
        tables = re.findall(r"(?:^\|.*\|$\n?)+", part, flags=re.MULTILINE)
        prose = part
        for table in tables:
            prose = prose.replace(table, "")
        pages.append(
            page_cls(
                page_number=index,
                text=prose.strip(),
                tables_markdown=[t.strip() for t in tables if t.strip()],
            )
        )
    return pages
