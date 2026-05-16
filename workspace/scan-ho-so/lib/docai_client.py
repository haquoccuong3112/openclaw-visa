"""Document AI OCR wrapper — trả list[{"page": N, "text": "..."}] per-page.

Hỗ trợ: PDF, JPEG, PNG, TIFF, BMP, WebP, GIF (native DocAI).
         DOC/DOCX: convert sang PDF trước qua LibreOffice headless.

Env vars (load từ scan-ocr.env trước khi gọi):
  GOOGLE_APPLICATION_CREDENTIALS — service account JSON path
  GOOGLE_DOCUMENTAI_PROJECT_ID   — vd "ally-visa-bot"
  GOOGLE_DOCUMENTAI_PROCESSOR_ID — vd "3183188b763e1843"
  GOOGLE_DOCUMENTAI_LOCATION     — vd "us"
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


DOCAI_PAGE_LIMIT = 15  # Document AI non-imageless mode: 15 trang/call

MIME_MAP: dict[str, str] = {
    ".pdf":  "application/pdf",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
    ".bmp":  "image/bmp",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}

OFFICE_EXTS = {".doc", ".docx"}


def convert_to_pdf(path: Path, workdir: Path) -> Path:
    """Convert .doc/.docx → PDF via LibreOffice headless. Trả path PDF tạm.

    Raises RuntimeError nếu LibreOffice chưa cài hoặc conversion thất bại.
    """
    if not shutil.which("libreoffice"):
        raise RuntimeError(
            "libreoffice chưa cài. Chạy: sudo apt-get install -y libreoffice-writer --no-install-recommends"
        )
    result = subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf",
         "--outdir", str(workdir), str(path)],
        check=True, timeout=60, capture_output=True,
    )
    pdf_path = workdir / (path.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError(
            f"LibreOffice chạy xong nhưng không thấy output PDF: {pdf_path}\n"
            f"stderr: {result.stderr.decode()[:500]}"
        )
    return pdf_path


def _ocr_chunk(raw_bytes: bytes, page_offset: int, mime_type: str,
               client, processor_name: str, documentai) -> list[dict]:
    """OCR 1 chunk (PDF ≤15 trang hoặc 1 image), trả list[{"page": N, "text": "..."}]."""
    from google.cloud import documentai as _dai  # noqa: PLC0415
    raw_doc = _dai.RawDocument(content=raw_bytes, mime_type=mime_type)
    request = _dai.ProcessRequest(name=processor_name, raw_document=raw_doc)
    result = client.process_document(request=request)
    doc = result.document
    full_text = doc.text or ""
    pages_out: list[dict] = []
    for page in doc.pages:
        page_no = page.page_number + page_offset  # 1-based tuyệt đối
        page_segs = page.layout.text_anchor.text_segments if page.layout else []
        page_text = "".join(
            full_text[int(s.start_index):int(s.end_index)] for s in page_segs
        ).strip()
        if not page_text:
            token_parts: list[str] = []
            for token in page.tokens:
                for seg in token.layout.text_anchor.text_segments:
                    token_parts.append(full_text[int(seg.start_index):int(seg.end_index)])
            page_text = "".join(token_parts).strip()
        pages_out.append({"page": page_no, "text": page_text})
    return pages_out


def ocr_with_docai(path: Path, workdir: Path | None = None) -> list[dict]:
    """OCR bất kỳ file (PDF, ảnh, hoặc Office doc) → list[{"page": N, "text": "..."}].

    - PDF/ảnh: gửi thẳng lên DocAI với MIME type tương ứng.
    - DOC/DOCX: convert sang PDF qua LibreOffice, rồi OCR như PDF.
    - PDF > 15 trang: tự split thành chunk ≤15, OCR từng chunk rồi gộp.
    - Image: luôn trả đúng 1 page với page=1.
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

    suffix = path.suffix.lower()
    tmp_pdf: Path | None = None

    try:
        # DOC/DOCX → convert to PDF first
        if suffix in OFFICE_EXTS:
            _workdir = workdir or Path(tempfile.mkdtemp())
            tmp_pdf = convert_to_pdf(path, _workdir)
            path = tmp_pdf
            suffix = ".pdf"

        mime_type = MIME_MAP.get(suffix, "application/pdf")

        # Images: single-page, send as-is
        if mime_type.startswith("image/"):
            return _ocr_chunk(path.read_bytes(), 0, mime_type, client, processor_name, documentai)

        # PDF: may need chunking
        try:
            import pypdf as _pypdf
            reader = _pypdf.PdfReader(str(path))
            n_total = len(reader.pages)
        except Exception:  # noqa: BLE001
            n_total = 0

        if n_total <= DOCAI_PAGE_LIMIT:
            return _ocr_chunk(path.read_bytes(), 0, mime_type, client, processor_name, documentai)

        # Long PDF → split into chunks
        import io
        import pypdf as _pypdf2
        reader2 = _pypdf2.PdfReader(str(path))
        pages_out: list[dict] = []
        chunk_start = 0
        while chunk_start < n_total:
            chunk_end = min(chunk_start + DOCAI_PAGE_LIMIT, n_total)
            writer = _pypdf2.PdfWriter()
            for i in range(chunk_start, chunk_end):
                writer.add_page(reader2.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            chunk_pages = _ocr_chunk(buf.getvalue(), chunk_start, mime_type,
                                     client, processor_name, documentai)
            pages_out.extend(chunk_pages)
            chunk_start = chunk_end
        return pages_out

    finally:
        # Clean up temp PDF from DOCX conversion
        if tmp_pdf and tmp_pdf.exists():
            tmp_pdf.unlink()


# Back-compat alias used by old code paths
ocr_pdf_with_docai = ocr_with_docai
