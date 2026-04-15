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

## Deploying to Azure

Deployments are automated via GitHub Actions — every push to `main` builds a new image and updates the Container App. Secrets must be configured in the GitHub repo settings (see below).

### First-time setup

Set these shell variables before running any `az` commands:

```bash
RESOURCE_GROUP="legal-intel-rg"
LOCATION="eastus"
ACR_NAME="legalintelacr"
ENVIRONMENT="legal-intel-env"
APP_NAME="pdf-service"
```

```bash
# 1. Create resource group
az group create --name $RESOURCE_GROUP --location $LOCATION

# 2. Create Azure Container Registry
az acr create --resource-group $RESOURCE_GROUP \
  --name $ACR_NAME --sku Basic --admin-enabled true

# 3. Create Container Apps environment
az containerapp env create \
  --name $ENVIRONMENT \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION

# 4. Build and push the initial image
az acr build --registry $ACR_NAME --image pdf-service:latest .

# 5. Deploy the Container App
ACR_PASSWORD=$(az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv)

az containerapp create \
  --name $APP_NAME \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image $ACR_NAME.azurecr.io/pdf-service:latest \
  --registry-server $ACR_NAME.azurecr.io \
  --registry-username $ACR_NAME \
  --registry-password $ACR_PASSWORD \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 5 \
  --cpu 1.0 --memory 2.0Gi

# 6. Get the public URL
az containerapp show \
  --name $APP_NAME \
  --resource-group $RESOURCE_GROUP \
  --query properties.configuration.ingress.fqdn -o tsv
```

### GitHub Actions secrets required

Add these in **GitHub → repo → Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `AZURE_CREDENTIALS` | JSON output from `az ad sp create-for-rbac` (see below) |
| `ACR_NAME` | e.g. `legalintelacr` |
| `CONTAINER_APP_NAME` | e.g. `pdf-service` |
| `RESOURCE_GROUP` | e.g. `legal-intel-rg` |

Generate `AZURE_CREDENTIALS`:

```bash
az ad sp create-for-rbac \
  --name "github-actions-legal-intel" \
  --role contributor \
  --scopes /subscriptions/$(az account show --query id -o tsv)/resourceGroups/$RESOURCE_GROUP \
  --sdk-auth
```

### Manual redeploy (if needed)

```bash
# Rebuild and push image
az acr build --registry $ACR_NAME --image pdf-service:latest .

# Update the running Container App
az containerapp update \
  --name $APP_NAME \
  --resource-group $RESOURCE_GROUP \
  --image $ACR_NAME.azurecr.io/pdf-service:latest
```

### Upgrade compute specs

```bash
az containerapp update \
  --name $APP_NAME \
  --resource-group $RESOURCE_GROUP \
  --cpu 2.0 --memory 4.0Gi
```

## License

MIT — see [LICENSE](LICENSE).

PyMuPDF is used under the [AGPL license](https://www.gnu.org/licenses/agpl-3.0.html). This service is open source, satisfying AGPL's network-service source-availability requirement.
