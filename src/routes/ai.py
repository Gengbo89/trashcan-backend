from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from src.config import settings
from src.services import ai as ai_service
from src.services.analytics import record_event
from src.services.auth import require_module
from src.services.security import check_image_security, check_text_security, looks_like_image
from src.services.storage import storage_service

router = APIRouter()
AI_IMAGE_DIR = 'ai-images'
AI_AUDIO_DIR = 'ai-audio'
IMAGE_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp', 'application/octet-stream'}
AUDIO_CONTENT_TYPES = {
    'audio/mpeg',
    'audio/mp3',
    'audio/wav',
    'audio/x-wav',
    'audio/mp4',
    'audio/m4a',
    'audio/aac',
    'audio/ogg',
    'application/octet-stream',
}


class ChatPayload(BaseModel):
    message: str
    history: list[dict[str, str]] = []
    model: str | None = None


class TextToImagePayload(BaseModel):
    prompt: str
    size: str | None = None
    model: str | None = None


def safe_filename(prefix: str, content_type: str = '') -> str:
    suffix_map = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/gif': '.gif',
        'image/bmp': '.bmp',
        'audio/mpeg': '.mp3',
        'audio/mp3': '.mp3',
        'audio/wav': '.wav',
        'audio/x-wav': '.wav',
        'audio/mp4': '.m4a',
        'audio/m4a': '.m4a',
        'audio/aac': '.aac',
        'audio/ogg': '.ogg',
    }
    return f'{prefix}-{uuid4().hex}{suffix_map.get(content_type, ".bin")}'


def normalize_chat_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    messages = []
    for item in history[-10:]:
        role = item.get('role')
        content = (item.get('content') or '').strip()
        if role in {'user', 'assistant'} and content:
            messages.append({'role': role, 'content': content[:4000]})
    return messages


def option_items(models: list[str]) -> list[dict[str, str]]:
    return [{'key': model, 'name': model, 'quota': ''} for model in models]


def model_options(config_value: str) -> list[dict[str, str]]:
    return option_items(settings.model_list(config_value))


def validate_model(model: str | None, config_value: str) -> str:
    allowed = settings.model_list(config_value)
    return validate_model_from_list(model, allowed)


def validate_model_from_list(model: str | None, allowed: list[str]) -> str:
    selected = (model or (allowed[0] if allowed else '')).strip()
    if not selected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='AI model is not configured')
    if selected not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported model')
    return selected


def upload_generated_image(image_url: str, openid: str) -> dict:
    data, content_type = ai_service.download_binary(image_url, settings.max_upload_size_bytes)
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Generated file is not a valid image')
    check_image_security(data, 'ai-generated.png', content_type)
    result = storage_service.upload_bytes(data, safe_filename('ai-image', content_type), content_type, AI_IMAGE_DIR)
    record_event(openid, 'ai_suite', 'image_store', True, metadata={'size': len(data)})
    return result


def raise_ai_error(exc: ai_service.DashScopeError):
    if isinstance(exc, ai_service.DashScopeFreeQuotaExhausted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                'code': 'AI_FREE_QUOTA_EXHAUSTED',
                'providerCode': exc.code,
                'message': exc.message,
            },
        ) from exc
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get('/models')
def ai_models(user=Depends(require_module('ai_suite'))):
    return {
        'code': 200,
        'success': True,
        'data': {
            'freeQuotaNote': '仅接入阿里百炼免费额度模型，免费额度用完即止。',
            'models': {
                'chat': {
                    'default': settings.default_model(settings.ai_chat_models),
                    'options': model_options(settings.ai_chat_models),
                },
                'textToImage': {
                    'default': settings.default_vision_model('textToImage'),
                    'options': option_items(settings.vision_model_list('textToImage')),
                },
                'imageToImage': {
                    'default': settings.default_vision_model('imageToImage'),
                    'options': option_items(settings.vision_model_list('imageToImage')),
                },
                'transcription': {
                    'default': settings.default_model(settings.ai_transcription_models),
                    'options': model_options(settings.ai_transcription_models),
                },
            },
            'capabilities': [
                {'key': 'chat', 'name': '对话', 'enabled': True},
                {'key': 'textToImage', 'name': '文生图', 'enabled': True},
                {'key': 'imageToImage', 'name': '图生图', 'enabled': True},
                {'key': 'transcription', 'name': '语音转写', 'enabled': True},
            ],
            'imageSize': settings.ai_image_size,
        },
    }


@router.post('/chat')
def ai_chat(payload: ChatPayload, user=Depends(require_module('ai_suite'))):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Message is required')
    if len(message) > 4000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Message is too long')
    check_text_security(message, user['openid'])
    model = validate_model(payload.model, settings.ai_chat_models)
    messages = [
        {
            'role': 'system',
            'content': '你是1号垃圾桶小程序里的AI工具助手，回答要简洁、准确、可操作。',
        },
        *normalize_chat_history(payload.history),
        {'role': 'user', 'content': message},
    ]
    try:
        data = ai_service.chat_completion(messages, model)
    except ai_service.DashScopeError as exc:
        record_event(user['openid'], 'ai_suite', 'chat', False, str(exc))
        raise_ai_error(exc)
    record_event(user['openid'], 'ai_suite', 'chat', True)
    return {'code': 200, 'success': True, 'data': data}


@router.post('/text-to-image')
def text_to_image(payload: TextToImagePayload, user=Depends(require_module('ai_suite'))):
    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Prompt is required')
    if len(prompt) > 800:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Prompt is too long')
    check_text_security(prompt, user['openid'])
    model = validate_model_from_list(payload.model, settings.vision_model_list('textToImage'))
    try:
        task_id = ai_service.start_image_task(model, {'prompt': prompt}, payload.size or settings.ai_image_size)
        image_url = ai_service.result_image_url(ai_service.poll_task(task_id))
        result = upload_generated_image(image_url, user['openid'])
    except ai_service.DashScopeError as exc:
        record_event(user['openid'], 'ai_suite', 'text_to_image', False, str(exc))
        raise_ai_error(exc)
    record_event(user['openid'], 'ai_suite', 'text_to_image', True)
    return {
        'code': 200,
        'success': True,
        'data': {
            'imageUrl': result['downloadUrl'],
            'downloadUrlExpiresIn': result['downloadUrlExpiresIn'],
            'downloadUrlExpiresAt': result['downloadUrlExpiresAt'],
            'objectKey': result['objectKey'],
            'model': model,
        },
    }


@router.post('/image-to-image')
async def image_to_image(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    size: str = Form(default=''),
    model: str = Form(default=''),
    user=Depends(require_module('ai_suite')),
):
    prompt = prompt.strip()
    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Prompt is required')
    check_text_security(prompt, user['openid'])
    content_type = file.content_type or 'application/octet-stream'
    if content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    data = await file.read(settings.max_upload_size_bytes + 1)
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Image is too large')
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    check_image_security(data, file.filename or 'input.png', content_type)
    selected_model = validate_model_from_list(model, settings.vision_model_list('imageToImage'))
    source = storage_service.upload_bytes(data, safe_filename('ai-source', content_type), content_type, AI_IMAGE_DIR)
    try:
        task_id = ai_service.start_image_task(
            selected_model,
            {'prompt': prompt, 'base_image_url': source['downloadUrl']},
            size or settings.ai_image_size,
        )
        image_url = ai_service.result_image_url(ai_service.poll_task(task_id))
        result = upload_generated_image(image_url, user['openid'])
    except ai_service.DashScopeError as exc:
        record_event(user['openid'], 'ai_suite', 'image_to_image', False, str(exc))
        raise_ai_error(exc)
    record_event(user['openid'], 'ai_suite', 'image_to_image', True)
    return {
        'code': 200,
        'success': True,
        'data': {
            'imageUrl': result['downloadUrl'],
            'downloadUrlExpiresIn': result['downloadUrlExpiresIn'],
            'downloadUrlExpiresAt': result['downloadUrlExpiresAt'],
            'objectKey': result['objectKey'],
            'model': selected_model,
        },
    }


@router.post('/transcribe')
async def transcribe_audio(
    file: UploadFile = File(...),
    model: str = Form(default=''),
    user=Depends(require_module('ai_suite')),
):
    content_type = file.content_type or 'application/octet-stream'
    if content_type not in AUDIO_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only audio files are supported')
    data = await file.read(settings.max_upload_size_bytes + 1)
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Audio is too large')
    source_name = Path(file.filename or '').name or safe_filename('audio', content_type)
    selected_model = validate_model(model, settings.ai_transcription_models)
    source = storage_service.upload_bytes(data, source_name, content_type, AI_AUDIO_DIR)
    try:
        task_id = ai_service.start_transcription_task(source['downloadUrl'], selected_model)
        text = ai_service.transcription_text(ai_service.poll_task(task_id, timeout_seconds=180))
    except ai_service.DashScopeError as exc:
        record_event(user['openid'], 'ai_suite', 'transcribe', False, str(exc))
        raise_ai_error(exc)
    record_event(user['openid'], 'ai_suite', 'transcribe', True, metadata={'size': len(data)})
    return {
        'code': 200,
        'success': True,
        'data': {
            'text': text,
            'model': selected_model,
        },
    }
