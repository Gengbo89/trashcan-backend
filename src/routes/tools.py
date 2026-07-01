import json
import ipaddress
import socket
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from src.config import settings
from src.services.auth import require_module
from src.services.storage import storage_service

router = APIRouter()
SESSION_ROOT = Path(tempfile.gettempdir()) / 'trashcan-upload-sessions'
IMAGE_PROXY_DIR = 'image-proxy'
IMAGE_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp'}


class ImageProxyPayload(BaseModel):
    url: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_session_dir(session_id: str) -> Path:
    if not session_id or '/' in session_id or '\\' in session_id or '..' in session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid sessionId')
    return SESSION_ROOT / session_id


def read_session_meta(session_dir: Path) -> list[dict]:
    meta_path = session_dir / 'meta.json'
    if not meta_path.exists():
        return []
    return json.loads(meta_path.read_text(encoding='utf-8'))


def write_session_meta(session_dir: Path, meta: list[dict]) -> None:
    (session_dir / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')


def assert_public_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only http/https image URLs are supported')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Image host cannot be resolved') from exc
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Private network image URLs are not allowed')
    return parsed


def filename_from_url(url: str, content_type: str) -> str:
    path_name = Path(urllib.parse.urlparse(url).path).name
    if path_name and '.' in path_name:
        return path_name
    suffix_map = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/webp': 'webp',
        'image/gif': 'gif',
        'image/bmp': 'bmp',
    }
    return f'image.{suffix_map.get(content_type, "bin")}'


def looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(b'\xff\xd8\xff')
        or data.startswith(b'\x89PNG\r\n\x1a\n')
        or data.startswith(b'GIF87a')
        or data.startswith(b'GIF89a')
        or data.startswith(b'BM')
        or (data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WEBP')
    )


def fetch_remote_image(url: str) -> tuple[str, bytes, str]:
    current_url = url
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        for _ in range(4):
            assert_public_url(current_url)
            request = urllib.request.Request(
                current_url,
                headers={
                    'User-Agent': 'trashcan-backend/1.0',
                    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
                },
                method='GET',
            )
            try:
                with opener.open(request, timeout=8) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in IMAGE_CONTENT_TYPES:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='URL does not point to a supported image')
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > settings.max_upload_size_bytes:
                        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Image is too large')
                    data = response.read(settings.max_upload_size_bytes + 1)
                    break
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                location = exc.headers.get('Location')
                if not location:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Image redirect has no target') from exc
                current_url = urllib.parse.urljoin(current_url, location)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Too many image redirects')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Image URL cannot be downloaded') from exc
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Image is too large')
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Downloaded file is not a valid image')
    return current_url, data, content_type


@router.post('/image-proxy', dependencies=[Depends(require_module('image_crop'))])
def proxy_image(payload: ImageProxyPayload):
    final_url, data, content_type = fetch_remote_image(payload.url)
    filename = filename_from_url(final_url, content_type)
    result = storage_service.upload_bytes(data, filename, content_type, IMAGE_PROXY_DIR)
    return {
        'code': 200,
        'success': True,
        'data': {
            'url': result['downloadUrl'],
            'filename': filename,
            'size': result['size'],
            'contentType': result['contentType'],
            'expiresIn': result['downloadUrlExpiresIn'],
            'expiresAt': result['downloadUrlExpiresAt'],
            'objectKey': result['objectKey'],
        },
    }


@router.post('/upload', dependencies=[Depends(require_module('file_transfer'))])
async def upload_tool_file(
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    upload_dir: str = Form(default='', alias='dir'),
    max_size_from_client: int | None = Form(default=None, alias='maxSize'),
    archive_format: str = Form(default='zip', alias='archiveFormat'),
):
    max_size = min(max_size_from_client or settings.max_upload_size_bytes, settings.max_upload_size_bytes)
    target_dir = upload_dir.strip().strip('/') or settings.default_upload_dir
    upload_files = files or ([file] if file else [])

    result = await storage_service.upload_files(
        files=upload_files,
        target_dir=target_dir,
        max_size=max_size,
        archive_format=archive_format,
    )

    return {
        'code': 200,
        'success': True,
        'data': result,
    }


@router.post('/upload-session', dependencies=[Depends(require_module('file_transfer'))])
def create_upload_session():
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    session_id = uuid4().hex
    session_dir = get_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=False)
    write_session_meta(session_dir, [])
    return {
        'code': 200,
        'success': True,
        'data': {'sessionId': session_id},
    }


@router.post('/upload-session/file', dependencies=[Depends(require_module('file_transfer'))])
async def upload_session_file(
    session_id: str = Form(alias='sessionId'),
    file: UploadFile = File(...),
    max_size_from_client: int | None = Form(default=None, alias='maxSize'),
):
    max_size = min(max_size_from_client or settings.max_upload_size_bytes, settings.max_upload_size_bytes)
    session_dir = get_session_dir(session_id)
    if not session_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Upload session not found')

    meta = read_session_meta(session_dir)
    current_size = sum(item['size'] for item in meta)
    remaining = max_size - current_size
    if remaining <= 0:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f'Upload exceeds {max_size} bytes')

    data = await file.read(remaining + 1)
    if current_size + len(data) > max_size:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f'Upload exceeds {max_size} bytes')

    stored_name = f'{len(meta)}-{uuid4().hex}.bin'
    stored_path = session_dir / stored_name
    stored_path.write_bytes(data)
    meta.append(
        {
            'name': file.filename or 'upload.bin',
            'storedName': stored_name,
            'size': len(data),
            'contentType': file.content_type or 'application/octet-stream',
        }
    )
    write_session_meta(session_dir, meta)

    return {
        'code': 200,
        'success': True,
        'data': {
            'sessionId': session_id,
            'fileCount': len(meta),
            'totalSize': current_size + len(data),
        },
    }


@router.post('/upload-session/complete', dependencies=[Depends(require_module('file_transfer'))])
def complete_upload_session(
    session_id: str = Form(alias='sessionId'),
    archive_format: str = Form(default='zip', alias='archiveFormat'),
    upload_dir: str = Form(default='', alias='dir'),
    max_size_from_client: int | None = Form(default=None, alias='maxSize'),
):
    max_size = min(max_size_from_client or settings.max_upload_size_bytes, settings.max_upload_size_bytes)
    target_dir = upload_dir.strip().strip('/') or settings.default_upload_dir
    session_dir = get_session_dir(session_id)
    if not session_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Upload session not found')

    meta = read_session_meta(session_dir)
    items = [
        (
            item['name'],
            (session_dir / item['storedName']).read_bytes(),
            item.get('contentType') or 'application/octet-stream',
        )
        for item in meta
    ]

    try:
        result = storage_service.upload_items(
            items=items,
            target_dir=target_dir,
            max_size=max_size,
            archive_format=archive_format,
            force_archive=True,
        )
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)

    return {
        'code': 200,
        'success': True,
        'data': result,
    }
