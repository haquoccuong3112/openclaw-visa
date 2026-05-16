"""Document AI OCR wrapper — trả list[{"page": N, "text": "..."}] per-page.

Env vars (load từ scan-ocr.env trước khi gọi):
  GOOGLE_APPLICATION_CREDENTIALS — service account JSON path
  GOOGLE_DOCUMENTAI_PROJECT_ID   — vd "ally-visa-bot"
  GOOGLE_DOCUMENTAI_PROCESSOR_ID — vd "3183188b763e1843"
  GOOGLE_DOCUMENTAI_LOCATION     — vd "us"
"""
from __future__ import annotations

import os
from pathlib import Path


DOCAI_PAGE_LIMIT = 15  # Document AI non-imageless mode: 15 trang/call (imageless = 30)


def _ocr_chunk(raw_bytes: bytes, page_offset: int,
               client, processor_name: str, documentai) -> list[dict]:
    """OCR 1 chunk PDF (≤15 trang), trả list[{"page": N, "text": "..."}] với page 1-based tuyệt đối."""
    from google.cloud import documentai as _dai  # noqa: PLC0415
    raw_doc = _dai.RawDocument(content=raw_bytes, mime_type="application/pdf")
    request = _dai.ProcessRequest(name=processor_name, raw_document=raw_doc)
    result = client.process_document(request=request)
    doc = result.document
    full_text = doc.text or ""
    pages_out: list[dict] = []
    for page in doc.pages:
        page_no = page.page_number + page_offset  # 1-based tuyệt đối
        # Dùng page-level text anchor (SDK-version agnostic, không phụ thuộc block.paragraphs)
        page_segs = page.layout.text_anchor.text_segments if page.layout else []
        page_text = "".join(
            full_text[int(s.start_index):int(s.end_index)] for s in page_segs
        ).strip()
        if not page_text:
            # Fallback: ghép từ tokens nếu có
            token_parts: list[str] = []
            for token in page.tokens:
                for seg in token.layout.text_anchor.text_segments:
                    token_parts.append(full_text[int(seg.start_index):int(seg.end_index)])
            page_text = "".join(token_parts).strip()
        pages_out.append({"page": page_no, "text": page_text})
    return pages_out


def ocr_pdf_with_docai(path: Path) -> list[dict]:
    """Gọi Document AI OCR 1 PDF → list[{"page": N, "text": "..."}] (1-based page).

    PDF > 30 trang tự động split thành chunk ≤30 trang, OCR từng chunk rồi gộp.
    Trả [] nếu lỗi cấu hình / API error (caller kiểm tra len()==0).
    """
    project_id   = os.environ.get("GOOGLE_DOCUMENTAI_PROJECT_ID", "")
    processor_id = os.environ.get("GOOGLE_DOCUMENTAI_PROCESSOR_ID", "")
    location     = os.environ.get("GOOGLE_DOCUMENTAI_LOCATION", "us")
    if not project_id or not processor_id:
        raise RuntimeError(
            "Thiếu GOOGLE_DOCUMENTAI_PROJECT_ID hoặc GOOGLE_DOCUMENTAI_PROCESSOR_ID trong env"
        )

    from google.cloud import documentai  # noqa: PLC0415
    from google.api_core.client_options import ClientOptions  # noqa: PLC0415

    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    processor_name = client.processor_path(project_id, location, processor_id)

    # Đếm trang để biết có cần split không
    try:
        import pypdf as _pypdf
        reader = _pypdf.PdfReader(str(path))
        n_total = len(reader.pages)
    except Exception:  # noqa: BLE001
        n_total = 0

    if n_total <= DOCAI_PAGE_LIMIT:
        # Gửi nguyên file
        return _ocr_chunk(path.read_bytes(), 0, client, processor_name, documentai)

    # PDF dài → split thành chunk ≤30 trang, OCR từng chunk
    import io
    import pypdf as _pypdf2
    reader2 = _pypdf2.PdfReader(str(path))
    pages_out: list[dict] = []
    chunk_start = 0  # 0-based
    while chunk_start < n_total:
        chunk_end = min(chunk_start + DOCAI_PAGE_LIMIT, n_total)  # exclusive
        writer = _pypdf2.PdfWriter()
        for i in range(chunk_start, chunk_end):
            writer.add_page(reader2.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunk_bytes = buf.getvalue()
        chunk_pages = _ocr_chunk(chunk_bytes, chunk_start, client, processor_name, documentai)
        pages_out.extend(chunk_pages)
        chunk_start = chunk_end

    return pages_out
