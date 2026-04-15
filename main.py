import atexit
import re
import os
import io
import logging
import uuid
from contextlib import asynccontextmanager

import fitz  # PyMuPDF — primary extractor: layout-aware, low memory, fast
import pdfplumber  # fallback: used when fitz fails to open a document
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from posthog import Posthog
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger("pdf_service")

# ---------------------------------------------------------------------------
# PostHog setup
# ---------------------------------------------------------------------------

posthog_client: Posthog | None = None


def _ph_capture(event: str, properties: dict) -> None:
    """Fire-and-forget PostHog capture. No-ops if client is not initialised."""
    if posthog_client is not None:
        posthog_client.capture(
            distinct_id=str(uuid.uuid4()),
            event=event,
            properties=properties,
        )


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global posthog_client
    token = os.environ.get("POSTHOG_PROJECT_TOKEN", "")
    host = os.environ.get("POSTHOG_HOST")
    if token:
        kwargs: dict = {"project_api_key": token, "enable_exception_autocapture": True}
        if host:
            kwargs["host"] = host
        posthog_client = Posthog(**kwargs)
        atexit.register(posthog_client.shutdown)
    yield
    if posthog_client is not None:
        posthog_client.flush()


app = FastAPI(title="Legal Intel PDF Service", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ExtractResponse(BaseModel):
    text: str
    is_scanned: bool
    page_count: int
    char_count: int


class ExtractFromUrlRequest(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Text cleaning helpers (library-agnostic)
# ---------------------------------------------------------------------------

# Indian Kanoon footer: "Indian Kanoon - http://indiankanoon.org/doc/123/\n42\n"
_IK_FOOTER = re.compile(
    r"Indian Kanoon - http://indiankanoon\.org/doc/\d+/\s*\n\s*\d*\s*\n",
    re.IGNORECASE,
)

# Hyphenation break: "legisla-\ntion" → "legislation"
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")

# More than two blank lines → two blank lines
_EXCESS_BLANK = re.compile(r"\n{3,}")


def clean_page_text(raw: str) -> str:
    text = _IK_FOOTER.sub("\n", raw)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _EXCESS_BLANK.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def _extract_with_pymupdf(contents: bytes) -> ExtractResponse:
    """
    Primary extractor. PyMuPDF (MuPDF under the hood) is written in C:
    - ~8x faster than pdfplumber for plain text
    - ~80% less peak memory (no full document model held in Python heap)
    - Layout-aware: get_text("text", sort=True) reads in visual order
      (top→bottom, left→right), handling multi-column and footnotes correctly

    fitz.open(stream=...) copies bytes into C-managed memory, so we can
    del contents immediately after open to free the Python-side buffer.
    """
    try:
        doc = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF could not open document: {exc}") from exc

    # Free the raw bytes — fitz holds its own C-side copy
    del contents

    if doc.is_encrypted:
        doc.close()
        raise HTTPException(
            status_code=422,
            detail="PDF is password-protected and cannot be processed.",
        )

    page_count = len(doc)
    if page_count == 0:
        doc.close()
        raise HTTPException(status_code=422, detail="PDF has no pages.")

    chunks: list[str] = []
    total_chars = 0

    try:
        for i in range(page_count):
            page = doc[i]
            # sort=True: sort text blocks by (y0, x0) → correct reading order
            # for multi-column layouts, footnotes, and header/body separation
            raw = page.get_text("text", sort=True)
            total_chars += len(raw)
            cleaned = clean_page_text(raw)
            if cleaned:
                chunks.append(f"[PAGE {i + 1}]\n{cleaned}")
    finally:
        doc.close()

    avg_chars = total_chars / page_count
    if avg_chars < 100:
        return ExtractResponse(
            text="", is_scanned=True, page_count=page_count, char_count=0
        )

    text = "\n\n".join(chunks)
    return ExtractResponse(
        text=text,
        is_scanned=False,
        page_count=page_count,
        char_count=len(text),
    )


def _extract_with_pdfplumber(contents: bytes) -> ExtractResponse:
    """
    Fallback extractor using pdfplumber (wraps pdfminer.six).
    Used only when PyMuPDF raises on open — e.g. unusual PDF constructs.
    Higher memory (~400 MB for large judgments) but handles edge cases
    that MuPDF's stricter parser rejects.
    """
    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            page_count = len(pdf.pages)
            if page_count == 0:
                raise HTTPException(status_code=422, detail="PDF has no pages.")

            chunks: list[str] = []
            total_chars = 0

            for i, page in enumerate(pdf.pages, start=1):
                raw = page.extract_text() or ""
                total_chars += len(raw)
                cleaned = clean_page_text(raw)
                if cleaned:
                    chunks.append(f"[PAGE {i}]\n{cleaned}")

            avg_chars = total_chars / page_count if page_count else 0
            if avg_chars < 100:
                return ExtractResponse(
                    text="", is_scanned=True, page_count=page_count, char_count=0
                )

            text = "\n\n".join(chunks)
            return ExtractResponse(
                text=text,
                is_scanned=False,
                page_count=page_count,
                char_count=len(text),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Failed to parse PDF: {exc}"
        ) from exc


def _extract_from_bytes(contents: bytes) -> ExtractResponse:
    """
    Attempt extraction with PyMuPDF; fall back to pdfplumber on failure.
    pdfplumber is retained because its pdfminer.six parser handles some
    non-standard or malformed PDFs that MuPDF's stricter parser rejects.
    """
    try:
        return _extract_with_pymupdf(contents)
    except HTTPException:
        raise  # 422 password-protected / no pages — don't retry with fallback
    except Exception as exc:
        logger.warning(
            "PyMuPDF extraction failed (%s) — falling back to pdfplumber", exc
        )
        _ph_capture("pdf_fallback_extractor_used", {"reason": str(exc)[:200]})
        return _extract_with_pdfplumber(contents)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "legal-intel-pdf-extractor", "version": "0.2.0"}


@app.post("/extract", response_model=ExtractResponse)
async def extract_pdf(file: UploadFile = File(...)):
    """Accept a multipart PDF upload and extract its text."""
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        _ph_capture("pdf_upload_failed", {"reason": "invalid_content_type"})
        raise HTTPException(
            status_code=400,
            detail=f"Expected a PDF file, got: {file.content_type}",
        )
    contents = await file.read()
    if not contents:
        _ph_capture("pdf_upload_failed", {"reason": "empty_file"})
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        result = _extract_from_bytes(contents)
    except HTTPException as exc:
        _ph_capture("pdf_upload_failed", {"reason": "parse_error", "status_code": exc.status_code})
        raise
    _ph_capture("pdf_extracted", {
        "page_count": result.page_count,
        "char_count": result.char_count,
        "is_scanned": result.is_scanned,
        "file_size_bytes": len(contents),
    })
    return result


@app.post("/extract-from-url", response_model=ExtractResponse)
async def extract_pdf_from_url(req: ExtractFromUrlRequest):
    """
    Download a PDF from a public URL and extract its text.
    The Python service pulls directly from the CDN (e.g. UploadThing) so
    the caller (Vercel) never re-transmits the file bytes.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(req.url)
            response.raise_for_status()
            contents = response.content
    except httpx.HTTPStatusError as exc:
        _ph_capture("pdf_url_extraction_failed", {
            "reason": "upstream_http_error",
            "upstream_status_code": exc.response.status_code,
        })
        raise HTTPException(
            status_code=400,
            detail=f"Failed to download PDF — upstream returned {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        _ph_capture("pdf_url_extraction_failed", {"reason": "request_error"})
        raise HTTPException(
            status_code=400,
            detail=f"Failed to download PDF from URL: {exc}",
        )

    if not contents:
        _ph_capture("pdf_url_extraction_failed", {"reason": "empty_download"})
        raise HTTPException(status_code=400, detail="Downloaded file is empty.")

    try:
        result = _extract_from_bytes(contents)
    except HTTPException as exc:
        _ph_capture("pdf_url_extraction_failed", {"reason": "parse_error", "status_code": exc.status_code})
        raise
    _ph_capture("pdf_url_extracted", {
        "page_count": result.page_count,
        "char_count": result.char_count,
        "is_scanned": result.is_scanned,
        "file_size_bytes": len(contents),
    })
    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
