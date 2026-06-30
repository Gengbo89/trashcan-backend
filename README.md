# Trashcan Backend

FastAPI backend for the WeChat Mini Program tool collection. The first supported tool uploads a file from the mini program to RustFS/S3-compatible object storage and returns a 1-hour presigned download URL.

## Features

- `GET /health` health check
- `POST /tools/upload` upload one file, default max size 10 MB
- Uploads to the configured RustFS bucket root by default
- Returns `downloadUrl` as a presigned temporary link for the mini program to display/copy
- Keeps RustFS credentials on the server side only

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Optional, for local TestClient checks:
# pip install -r requirements-dev.txt
cp .env.example .env
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

```bash
curl http://127.0.0.1:8000/health
```

## Upload API

```http
POST /tools/upload
Content-Type: multipart/form-data

file: <binary>
maxSize: 10485760
```

Response:

```json
{
  "code": 200,
  "success": true,
  "data": {
    "downloadUrl": "https://rustfs.gengbo.top/<bucket>/1700000000-file.pdf?X-Amz-Algorithm=...",
    "downloadUrlExpiresIn": 3600,
    "downloadUrlExpiresAt": "2026-06-30T12:00:00+00:00",
    "objectKey": "1700000000-file.pdf",
    "bucket": "<bucket>"
  }
}
```

## Environment

Copy `.env.example` to `.env` and set real values on the server. `PRESIGNED_URL_EXPIRES_SECONDS=3600` makes returned download links valid for 1 hour. Leave `DEFAULT_UPLOAD_DIR` empty to upload directly to the bucket root.

Important: do not put RustFS admin credentials in the mini program. The mini program should only call this backend domain, for example `https://trashcan.gengbo.top/tools/upload`.

## Deploy Notes

1. Point `trashcan.gengbo.top` to this service through Nginx/Caddy/Ingress.
2. Configure HTTPS, because WeChat Mini Programs require HTTPS request domains.
3. Add `https://trashcan.gengbo.top` to the mini program legal request/upload domain list in WeChat MP admin.
4. Set `config.baseUrl = 'https://trashcan.gengbo.top'` and `config.isMock = false` in the mini program when switching to real backend.

## Docker

```bash
docker build -t trashcan-backend .
docker run --env-file .env -p 8000:8000 trashcan-backend
```
