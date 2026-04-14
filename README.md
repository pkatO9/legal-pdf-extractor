# legal-pdf-extractor

A lightweight FastAPI service that extracts text from PDF documents. Built for legal judgments but works on any text-based PDF.

## What it does

- Accepts a PDF via direct upload (`/extract`) or a public URL (`/extract-from-url`)
- Extracts text using **PyMuPDF** (primary) with **pdfplumber** as a fallback
- Returns page-marked text (`[PAGE N]`), scanned-document detection, and character count
- Cleans hyphenation breaks, Indian Kanoon footers, and excess whitespace

## Why two extractors

PyMuPDF (MuPDF under the hood) is ~8× faster than pdfplumber and uses ~80% less memory while remaining layout-aware — it reads text in visual order, which matters for multi-column documents, tables, and footnotes. pdfplumber (pdfminer.six) is kept as a fallback for non-standard PDFs that MuPDF's stricter parser rejects.

## API

### `GET /health`
```json
{ "status": "ok", "service": "legal-intel-pdf-extractor", "version": "0.2.0" }
```

### `POST /extract`
Accepts `multipart/form-data` with a `file` field (PDF).

### `POST /extract-from-url`
Accepts `application/json` — `{ "url": "https://..." }`. The service downloads the PDF directly, avoiding the caller re-transmitting the file bytes.

**Response (both extract endpoints):**
```json
{
  "text": "[PAGE 1]\n...\n\n[PAGE 2]\n...",
  "is_scanned": false,
  "page_count": 42,
  "char_count": 187432
}
```
If `is_scanned` is `true`, `text` is empty and the document likely needs OCR.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Interactive docs at http://localhost:8000/docs

## Running with Docker

```bash
docker build -t legal-pdf-extractor .
docker run -p 8000:8000 legal-pdf-extractor
```

## Testing

```bash
# Health check
curl http://localhost:8000/health

# Extract from file
curl -X POST http://localhost:8000/extract \
  -F "file=@/path/to/judgment.pdf" | jq .

# Extract from URL
curl -X POST http://localhost:8000/extract-from-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/judgment.pdf"}' | jq .
```

## Deploying to Render

A `render.yaml` is included. Connect this repo to Render and it will deploy automatically on every push.

## License

MIT — see [LICENSE](LICENSE).

PyMuPDF is used under the [AGPL license](https://www.gnu.org/licenses/agpl-3.0.html). This service is open source, satisfying AGPL's network-service source-availability requirement.
