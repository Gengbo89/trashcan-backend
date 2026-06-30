import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.config import settings
from src.services.auth import require_module
from src.services.storage import storage_service

router = APIRouter(dependencies=[Depends(require_module('file_transfer'))])
SESSION_ROOT = Path(tempfile.gettempdir()) / 'trashcan-upload-sessions'


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


@router.post('/upload')
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


@router.post('/upload-session')
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


@router.post('/upload-session/file')
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


@router.post('/upload-session/complete')
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
