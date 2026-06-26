from fastapi import APIRouter, File, Form, UploadFile

from src.config import settings
from src.services.storage import storage_service

router = APIRouter()


@router.post('/upload')
async def upload_tool_file(
    file: UploadFile = File(...),
    upload_dir: str = Form(default='', alias='dir'),
    max_size_from_client: int | None = Form(default=None, alias='maxSize'),
):
    max_size = min(max_size_from_client or settings.max_upload_size_bytes, settings.max_upload_size_bytes)
    target_dir = upload_dir.strip().strip('/') or settings.default_upload_dir

    result = await storage_service.upload_file(
        file=file,
        target_dir=target_dir,
        max_size=max_size,
    )

    return {
        'code': 200,
        'success': True,
        'data': result,
    }
