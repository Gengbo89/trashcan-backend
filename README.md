# Trashcan Backend

FastAPI backend for the WeChat Mini Program tool collection. The first supported tool uploads a file from the mini program to RustFS/S3-compatible object storage and returns a 1-hour presigned download URL.

## Features

- `GET /health` health check
- `POST /tools/upload` upload one or more files, default max size 10 MB per request
- Uploads one file directly, or archives multiple files as `zip`/`tar.gz` before uploading
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

file: <binary>       # single file
files: <binary>[]   # multiple files, same field name repeated
maxSize: 10485760
archiveFormat: zip | tar.gz
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
    "bucket": "<bucket>",
    "mode": "single | archive",
    "archiveFormat": "zip"
  }
}
```

## Environment

Copy `.env.example` to `.env` and set real values on the server. `PRESIGNED_URL_EXPIRES_SECONDS=3600` makes returned download links valid for 1 hour. Leave `DEFAULT_UPLOAD_DIR` empty to upload directly to the bucket root.

Important: do not put RustFS admin credentials in the mini program. The mini program should only call this backend domain, for example `https://trashcan.gengbo.top/tools/upload`.


## WeChat Login & Permissions

Set these environment variables in production:

```env
WECHAT_APPID=your-mini-program-appid
WECHAT_SECRET=your-mini-program-secret
WECHAT_MESSAGE_TEMPLATE_ID=yrnDr2o4chCcJTxEcZm59BThRMrZ3rkt4oXZlHzakus
JWT_SECRET=use-a-long-random-string
ADMIN_OPENIDS=openid1,openid2
DATABASE_URL=postgresql://trashcan:trashcan@postgres:5432/trashcan
DASHSCOPE_API_KEY=your-bailian-api-key
AI_CHAT_MODELS=qwen-turbo,qwen-plus,qwen-long
AI_VISION_MODELS=wanx2.1-t2i-turbo:textToImage,wanx2.1-imageedit:imageToImage
AI_TRANSCRIPTION_MODELS=paraformer-v2
```

The first logged-in user is also promoted to admin automatically, which is convenient for initial setup. Admins can open the mini program's `我的 -> 权限管理` page to enable or disable modules for each user. The upload APIs require the `file_transfer` module permission.

## AI Tools

AI tools are proxied by the backend under `/ai/*`, so the mini program never stores the Alibaba Cloud Model Studio API key. The model list is configured with comma-separated `AI_*_MODELS` values. The first model in each list is used as the default model shown in the mini program. Visual models share `AI_VISION_MODELS`; add a capability suffix such as `:textToImage`, `:imageToImage`, or `:textToImage|imageToImage` to control which mini program entry can select the model.

Generated images are downloaded by the backend, checked by the existing image safety flow, uploaded to RustFS, and returned as 1-hour presigned links.

When Alibaba Cloud Model Studio returns `AllocationQuota.FreeTierOnly`, the backend responds with HTTP 403 and `AI_FREE_QUOTA_EXHAUSTED`. The mini program shows this as free quota exhausted instead of a generic AI failure.

## Message Reminders

In-app messages are pushed through WebSocket at `/messages/ws` when the user is online. If an admin replies while the user is offline, the backend falls back to WeChat subscribe messages.

Offline reminders use template `yrnDr2o4chCcJTxEcZm59BThRMrZ3rkt4oXZlHzakus` by default. The current payload uses `time2` for the send time, `number1` for the user's unread message count, and `thing3` for `您有新的未读消息，请注意查收`.

## Deploy Notes

1. Point `trashcan.gengbo.top` to this service through Nginx/Caddy/Ingress.
2. Configure HTTPS, because WeChat Mini Programs require HTTPS request domains.
3. Add `https://trashcan.gengbo.top` to the mini program legal request/upload domain list in WeChat MP admin.
4. Add `wss://trashcan.gengbo.top` to the mini program legal socket domain list in WeChat MP admin.
5. Set `config.baseUrl = 'https://trashcan.gengbo.top'` and `config.isMock = false` in the mini program when switching to real backend.

## Docker

```bash
docker build -t trashcan-backend .
docker run --env-file .env -p 8000:8000 trashcan-backend
```
