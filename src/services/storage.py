import io
import tarfile
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import boto3
from botocore.client import Config
from fastapi import HTTPException, UploadFile, status

from src.config import settings


ARCHIVE_FORMATS = {'zip', 'tar.gz'}
UploadItem = tuple[str, bytes, str]


def sanitize_filename(filename: str) -> str:
    safe = filename.replace('\\', '/').split('/')[-1].strip()
    return safe or 'upload.bin'


def normalize_dir(target_dir: str) -> str:
    normalized = target_dir.replace('\\', '/')
    parts = [part for part in normalized.split('/') if part and part not in {'.', '..'}]
    return '/'.join(parts)


def unique_archive_name(filename: str, used_names: set[str]) -> str:
    base = sanitize_filename(filename)
    if base not in used_names:
        used_names.add(base)
        return base

    stem, dot, suffix = base.rpartition('.')
    if not dot:
        stem, suffix = base, ''
    else:
        suffix = f'.{suffix}'

    index = 2
    while True:
        candidate = f'{stem}-{index}{suffix}'
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        index += 1


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

    def build_object_key(self, filename: str, target_dir: str) -> str:
        prefix = normalize_dir(target_dir)
        object_name = f'{int(time.time() * 1000)}-{sanitize_filename(filename)}'
        return str(PurePosixPath(prefix) / object_name) if prefix else object_name

    def upload_bytes(self, data: bytes, filename: str, content_type: str, target_dir: str) -> dict:
        object_key = self.build_object_key(filename, target_dir)
        self.client.put_object(
            Bucket=settings.rustfs_bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )

        expires_in = settings.presigned_url_expires_seconds
        download_url = self.client.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': settings.rustfs_bucket,
                'Key': object_key,
            },
            ExpiresIn=expires_in,
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        return {
            'downloadUrl': download_url,
            'downloadUrlExpiresIn': expires_in,
            'downloadUrlExpiresAt': expires_at.isoformat(),
            'objectKey': object_key,
            'bucket': settings.rustfs_bucket,
            'size': len(data),
            'contentType': content_type,
        }

    async def read_uploads(self, files: list[UploadFile], max_size: int) -> list[UploadItem]:
        items = []
        total_size = 0
        for file in files:
            remaining = max_size - total_size
            if remaining <= 0:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f'Upload exceeds {max_size} bytes',
                )
            data = await file.read(remaining + 1)
            total_size += len(data)
            if total_size > max_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f'Upload exceeds {max_size} bytes',
                )
            items.append((sanitize_filename(file.filename or 'upload.bin'), data, file.content_type or 'application/octet-stream'))
        return items

    def build_archive(self, items: list[UploadItem], archive_format: str) -> tuple[str, bytes, str]:
        if archive_format not in ARCHIVE_FORMATS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='archiveFormat must be zip or tar.gz',
            )

        used_names: set[str] = set()
        archive_name = f'files-{int(time.time() * 1000)}.{archive_format}'
        buffer = io.BytesIO()

        if archive_format == 'zip':
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for filename, data, _ in items:
                    archive.writestr(unique_archive_name(filename, used_names), data)
            content_type = 'application/zip'
        else:
            with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                for filename, data, _ in items:
                    arcname = unique_archive_name(filename, used_names)
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    info.mtime = int(time.time())
                    archive.addfile(info, io.BytesIO(data))
            content_type = 'application/gzip'

        return archive_name, buffer.getvalue(), content_type

    def upload_items(
        self,
        items: list[UploadItem],
        target_dir: str,
        max_size: int,
        archive_format: str = 'zip',
        force_archive: bool = False,
    ) -> dict:
        if not items:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='No file uploaded')

        total_size = sum(len(data) for _, data, _ in items)
        if total_size > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f'Upload exceeds {max_size} bytes',
            )

        if len(items) == 1 and not force_archive:
            filename, data, content_type = items[0]
            result = self.upload_bytes(data, filename, content_type, target_dir)
            result['mode'] = 'single'
            return result

        archive_name, archive_data, content_type = self.build_archive(items, archive_format)
        if len(archive_data) > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f'Archive exceeds {max_size} bytes',
            )

        result = self.upload_bytes(archive_data, archive_name, content_type, target_dir)
        result.update(
            {
                'mode': 'archive',
                'archiveFormat': archive_format,
                'fileCount': len(items),
            }
        )
        return result

    async def upload_files(
        self,
        files: list[UploadFile],
        target_dir: str,
        max_size: int,
        archive_format: str = 'zip',
    ) -> dict:
        return self.upload_items(
            items=await self.read_uploads(files, max_size),
            target_dir=target_dir,
            max_size=max_size,
            archive_format=archive_format,
        )


storage_service = StorageService()
