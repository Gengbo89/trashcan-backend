import time
from pathlib import PurePosixPath
from urllib.parse import quote

import boto3
from botocore.client import Config
from fastapi import HTTPException, UploadFile, status

from src.config import settings


def sanitize_filename(filename: str) -> str:
    safe = filename.replace('\\', '/').split('/')[-1].strip()
    return safe or 'upload.bin'


def normalize_dir(target_dir: str) -> str:
    normalized = target_dir.replace('\\', '/')
    parts = [part for part in normalized.split('/') if part and part not in {'.', '..'}]
    return '/'.join(parts)


def build_public_url(object_key: str) -> str:
    base = settings.rustfs_public_base_url.rstrip('/')
    encoded_key = '/'.join(quote(part) for part in object_key.split('/'))
    return f'{base}/{encoded_key}'


class StorageService:
    def __init__(self):
        self.client = boto3.client(
            's3',
            endpoint_url=settings.rustfs_endpoint_url,
            aws_access_key_id=settings.rustfs_access_key,
            aws_secret_access_key=settings.rustfs_secret_key,
            region_name=settings.rustfs_region,
            config=Config(s3={'addressing_style': settings.rustfs_addressing_style}),
        )

    async def upload_file(self, file: UploadFile, target_dir: str, max_size: int) -> dict:
        filename = sanitize_filename(file.filename or 'upload.bin')
        prefix = normalize_dir(target_dir)
        object_name = f'{int(time.time() * 1000)}-{filename}'
        object_key = str(PurePosixPath(prefix) / object_name) if prefix else object_name

        data = await file.read(max_size + 1)
        if len(data) > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f'File exceeds {max_size} bytes',
            )

        content_type = file.content_type or 'application/octet-stream'
        self.client.put_object(
            Bucket=settings.rustfs_bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )

        return {
            'downloadUrl': build_public_url(object_key),
            'objectKey': object_key,
            'bucket': settings.rustfs_bucket,
            'size': len(data),
            'contentType': content_type,
        }


storage_service = StorageService()
