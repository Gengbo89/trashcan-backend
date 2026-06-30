from fastapi import APIRouter, File, Form, UploadFile

from src.config import settings
from src.services.storage import storage_service

router = APIRouter()


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
