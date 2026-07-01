import json
import ipaddress
import socket
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
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
DOCUMENT_CONVERT_DIR = 'document-convert'
QRCODE_DIR = 'qrcode'
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx'}


class ImageProxyPayload(BaseModel):
    url: str


class QrCodePayload(BaseModel):
    text: str


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


def file_stem(filename: str) -> str:
    return Path(filename.replace('\\', '/').split('/')[-1] or 'file').stem or 'file'


def file_suffix(filename: str) -> str:
    return Path(filename.replace('\\', '/').split('/')[-1]).suffix.lower()


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


def convert_pdf_to_docx(input_path: Path, output_path: Path) -> None:
    try:
        from pdf2docx import Converter
    except ImportError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='pdf2docx is not installed') from exc

    converter = Converter(str(input_path))
    try:
        converter.convert(str(output_path), start=0, end=None)
    finally:
        converter.close()


def convert_word_to_pdf(input_path: Path, output_dir: Path) -> Path:
    try:
        subprocess.run(
            [
                settings.office_bin,
                '--headless',
                '--convert-to',
                'pdf',
                '--outdir',
                str(output_dir),
                str(input_path),
            ],
            check=True,
            timeout=60,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='soffice/libreoffice is not installed') from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail='Document conversion timed out') from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Document conversion failed') from exc

    output_path = output_dir / f'{input_path.stem}.pdf'
    if not output_path.exists():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Converted PDF was not generated')
    return output_path


@router.post('/document-convert', dependencies=[Depends(require_module('document_convert'))])
async def convert_document(file: UploadFile = File(...), target_format: str = Form(alias='targetFormat')):
    source_name = file.filename or 'document'
    source_ext = file_suffix(source_name)
    target_format = target_format.lower().strip()
    if source_ext not in DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only PDF, DOC and DOCX files are supported')
    if source_ext == '.pdf' and target_format != 'docx':
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='PDF can only be converted to DOCX')
    if source_ext in {'.doc', '.docx'} and target_format != 'pdf':
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Word files can only be converted to PDF')

    data = await file.read(settings.max_upload_size_bytes + 1)
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Document is too large')

    with tempfile.TemporaryDirectory(prefix='trashcan-doc-convert-') as temp_dir:
        work_dir = Path(temp_dir)
        input_path = work_dir / f'input{source_ext}'
        input_path.write_bytes(data)
        if source_ext == '.pdf':
            output_path = work_dir / f'{file_stem(source_name)}.docx'
            convert_pdf_to_docx(input_path, output_path)
            output_name = output_path.name
            content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        else:
            output_path = convert_word_to_pdf(input_path, work_dir)
            output_name = f'{file_stem(source_name)}.pdf'
            content_type = 'application/pdf'

        output_data = output_path.read_bytes()
        if not output_data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Converted document is empty')
        if len(output_data) > settings.max_upload_size_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Converted document is too large')
        result = storage_service.upload_bytes(output_data, output_name, content_type, DOCUMENT_CONVERT_DIR)

    return {
        'code': 200,
        'success': True,
        'data': {
            'downloadUrl': result['downloadUrl'],
            'downloadUrlExpiresIn': result['downloadUrlExpiresIn'],
            'downloadUrlExpiresAt': result['downloadUrlExpiresAt'],
            'filename': output_name,
            'size': len(output_data),
            'contentType': content_type,
            'objectKey': result['objectKey'],
        },
    }


@router.post('/qrcode/generate', dependencies=[Depends(require_module('qrcode'))])
def generate_qrcode(payload: QrCodePayload):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='QR content is required')
    if len(text) > 2048:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='QR content is too long')
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='qrcode is not installed') from exc

    image = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    image.add_data(text)
    image.make(fit=True)
    png = image.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    png.save(buffer, format='PNG')
    data = buffer.getvalue()
    result = storage_service.upload_bytes(data, f'qrcode-{uuid4().hex}.png', 'image/png', QRCODE_DIR)
    return {
        'code': 200,
        'success': True,
        'data': {
            'imageUrl': result['downloadUrl'],
            'downloadUrlExpiresIn': result['downloadUrlExpiresIn'],
            'downloadUrlExpiresAt': result['downloadUrlExpiresAt'],
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
