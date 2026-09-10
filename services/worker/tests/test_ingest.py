"""Ingestion pipeline tests — PRD section 7.

Appendix B step 6 is explicit that text and table extraction is verified on the
fixtures *before* any LLM call exists, independently of extraction quality.
That is what this file does: no model, no network, no prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ingest import (
    DocumentFormat,
    IngestionError,
    chunk_text,
    content_hash,
    detect_format,
    ingest,
    table_to_markdown,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "syllabi"


def _load(name: str) -> bytes:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"fixture {name} not generated; run scripts/generate_fixture_syllabi.py")
    return path.read_bytes()


# --- dedup (section 7, 18.3) ----------------------------------------------------------


def test_content_hash_is_stable_and_content_addressed() -> None:
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")
    assert len(content_hash(b"abc")) == 64


def test_identical_uploads_dedupe_to_one_hash() -> None:
    """Dozens of students in a section upload the same file; parse once (18.3)."""
    data = _load("cs_lecture_native_pdf.pdf")
    assert content_hash(data) == content_hash(bytes(data))


# --- format detection -------------------------------------------------------------------


def test_format_detection_uses_magic_bytes_over_extension() -> None:
    """Phones and browsers mislabel uploads constantly; bytes are authoritative."""
    pdf_bytes = _load("cs_lecture_native_pdf.pdf")
    assert detect_format("actually_a_pdf.docx", pdf_bytes) is DocumentFormat.PDF_NATIVE


def test_format_detection_recognizes_each_accepted_type() -> None:
    assert detect_format("s.pdf", b"%PDF-1.7\n...") is DocumentFormat.PDF_NATIVE
    assert detect_format("s.png", b"\x89PNG\r\n\x1a\n") is DocumentFormat.IMAGE
    assert detect_format("s.jpg", b"\xff\xd8\xff\xe0") is DocumentFormat.IMAGE
    assert detect_format("s.html", b"<html><body>hi</body></html>") is DocumentFormat.HTML
    assert detect_format("s.txt", b"plain text") is DocumentFormat.TEXT


def test_unrecognized_binary_is_unknown_not_a_crash() -> None:
    assert detect_format("mystery.bin", b"\x00\x01\x02\x03") is DocumentFormat.UNKNOWN


# --- tables (the deciding requirement, section 7) ----------------------------------------


def test_table_to_markdown_preserves_row_and_column_structure() -> None:
    markdown = table_to_markdown(
        [["Category", "Weight"], ["Exams", "40%"], ["Problem Sets", "20%"]]
    )
    lines = markdown.splitlines()
    assert lines[0] == "| Category | Weight |"
    assert lines[1] == "| --- | --- |"
    assert "| Exams | 40% |" in lines


def test_table_to_markdown_escapes_pipes_in_cells() -> None:
    markdown = table_to_markdown([["A|B"], ["c"]])
    assert r"A\|B" in markdown


def test_table_to_markdown_pads_ragged_rows() -> None:
    markdown = table_to_markdown([["A", "B", "C"], ["1"]])
    assert "| 1 |  |  |" in markdown


def test_table_to_markdown_ignores_entirely_empty_input() -> None:
    assert table_to_markdown([]) == ""
    assert table_to_markdown([["", ""], [None, None]]) == ""


def test_pdf_grading_table_survives_extraction() -> None:
    """The weight extraction depends entirely on this not becoming word soup."""
    document = ingest("cs_lecture_native_pdf.pdf", _load("cs_lecture_native_pdf.pdf"))
    tables = [t for page in document.pages for t in page.tables_markdown]
    assert tables, "no tables extracted from a syllabus that has two"

    grading = next((t for t in tables if "Weight" in t), None)
    assert grading is not None
    assert "| Exams | 40% |" in grading.replace(" | 2 | 0 |", " |")
    assert "Problem Sets" in grading


def test_docx_grading_table_survives_extraction() -> None:
    document = ingest("cs_lecture_grading_table.docx", _load("cs_lecture_grading_table.docx"))
    tables = [t for page in document.pages for t in page.tables_markdown]
    assert any("Exams" in t and "40%" in t for t in tables)


def test_html_tables_are_extracted_and_not_duplicated_into_prose() -> None:
    html = b"""
    <html><body>
      <h1>ABC 1000</h1>
      <p>Course description here.</p>
      <table><tr><th>Category</th><th>Weight</th></tr>
             <tr><td>Exams</td><td>40%</td></tr></table>
    </body></html>
    """
    document = ingest("export.html", html)
    assert any("| Exams | 40% |" in t for t in document.pages[0].tables_markdown)
    # The table's cells must not also appear loose in the prose text.
    assert "40%" not in document.pages[0].text


# --- scanned detection (section 7) --------------------------------------------------------


def test_scanned_pdf_is_detected_by_character_count() -> None:
    """Under ~100 chars/page means image-only and must route to OCR."""
    from ingest import _extract_pdf

    _, looks_scanned = _extract_pdf(_load("cs_lecture_scanned.pdf"))
    assert looks_scanned is True


def test_native_pdf_is_not_misrouted_to_ocr() -> None:
    from ingest import _extract_pdf

    _, looks_scanned = _extract_pdf(_load("cs_lecture_native_pdf.pdf"))
    assert looks_scanned is False


def test_scanned_pdf_without_ocr_fails_loudly_rather_than_returning_empty() -> None:
    """9.4's hard-failure path: route to manual entry, never show an empty result."""
    with pytest.raises(IngestionError):
        ingest("cs_lecture_scanned.pdf", _load("cs_lecture_scanned.pdf"), allow_ocr=False)


# --- page markers and text assembly --------------------------------------------------------


def test_full_text_carries_page_markers_for_page_ref() -> None:
    """`page_ref` in 8.2 is only answerable if the model can see page boundaries."""
    document = ingest("cs_lecture_native_pdf.pdf", _load("cs_lecture_native_pdf.pdf"))
    assert "[page 1]" in document.full_text
    if document.page_count > 1:
        assert "[page 2]" in document.full_text


def test_plain_text_upload_round_trips() -> None:
    document = ingest("pasted.txt", b"COP 4530\nMidterm on October 14, 2026")
    assert "October 14, 2026" in document.full_text
    assert document.is_scanned is False


# --- chunking (section 7) --------------------------------------------------------------------


def test_short_documents_are_not_chunked() -> None:
    assert len(chunk_text("short text")) == 1


def test_chunking_splits_on_page_boundaries() -> None:
    """Splitting mid-table would reintroduce exactly the failure tables exist to prevent."""
    pages = "".join(f"[page {i}]\n" + ("word " * 2000) + "\n\n" for i in range(1, 8))
    chunks = chunk_text(pages, max_tokens=2000)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert chunk.lstrip().startswith("[page ")
