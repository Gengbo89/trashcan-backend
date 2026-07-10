import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from src.config import settings
from src.services import ai as ai_service
from src.services.analytics import record_event
from src.services.auth import require_module
from src.services.security import check_image_security, check_text_security, looks_like_image
from src.services.storage import storage_service

logger = logging.getLogger(__name__)

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
    'audio/x-m4a',
    'audio/aac',
    'audio/ogg',
    'video/mp4',
    'application/octet-stream',
}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.mp4'}
AUTO_MODEL_KEY = '__auto__'
_MODEL_CACHE: dict[str, str] = {}
_EXHAUSTED_MODELS: dict[str, set[str]] = {}


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


def auto_option_items(models: list[str]) -> list[dict[str, str]]:
    return [{'key': AUTO_MODEL_KEY, 'name': '自动选择', 'quota': '后端按可用模型自动尝试'}, *option_items(models)]


def model_options(config_value: str) -> list[dict[str, str]]:
    return option_items(settings.model_list(config_value))


def vision_models(capability: str) -> list[str]:
    return settings.vision_model_list()


def validate_model(model: str | None, config_value: str) -> str:
    allowed = settings.model_list(config_value)
    return validate_model_from_list(model, allowed)


def validate_model_from_list(model: str | None, allowed: list[str]) -> str:
    selected = (model or (allowed[0] if allowed else '')).strip()
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'code': 'AI_MODEL_NOT_CONFIGURED',
                'message': '未配置可用模型，请按大类配置 AI_LLM_MODELS、AI_VISION_MODELS 或 AI_AUDIO_MODELS。',
            },
        )
    if selected not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported model')
    return selected


def mark_model_exhausted(capability: str, model: str):
    _EXHAUSTED_MODELS.setdefault(capability, set()).add(model)
    if _MODEL_CACHE.get(capability) == model:
        _MODEL_CACHE.pop(capability, None)
    logger.warning('ai_model_exhausted capability=%s model=%s', capability, model)


def ordered_candidates(capability: str, requested_model: str | None, allowed: list[str]) -> list[str]:
    exhausted = _EXHAUSTED_MODELS.get(capability, set())
    requested = (requested_model or '').strip()
    if requested and requested != AUTO_MODEL_KEY:
        selected = validate_model_from_list(requested, allowed)
        return [] if selected in exhausted else [selected]
    cached = _MODEL_CACHE.get(capability)
    candidates = []
    if cached in allowed and cached not in exhausted:
        candidates.append(cached)
    candidates.extend(model for model in allowed if model not in candidates and model not in exhausted)
    if not candidates:
        if allowed and exhausted.issuperset(allowed):
            raise ai_service.DashScopeFreeQuotaExhausted('阿里百炼免费额度已用尽，平台已自动停止服务。', 'AllocationQuota.FreeTierOnly')
        validate_model_from_list('', allowed)
    return candidates


def upload_generated_image(image_url: str, openid: str) -> dict:
    logger.info('ai_upload_generated_image_start openid=%s source_host=%s', openid, urlparse(image_url).netloc)
    data, content_type = ai_service.download_binary(image_url, settings.max_upload_size_bytes)
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Generated file is not a valid image')
    check_image_security(data, 'ai-generated.png', content_type)
    result = storage_service.upload_bytes(data, safe_filename('ai-image', content_type), content_type, AI_IMAGE_DIR)
    logger.info(
        'ai_upload_generated_image_success openid=%s content_type=%s bytes=%s object_key=%s download_host=%s',
        openid,
        content_type,
        len(data),
        result.get('objectKey'),
        urlparse(result.get('downloadUrl', '')).netloc,
    )
    record_event(openid, 'ai_suite', 'image_store', True, metadata={'size': len(data)})
    return result


def looks_like_audio(data: bytes) -> bool:
    head = data[:32]
    return (
        head.startswith(b'ID3')
        or head.startswith(b'RIFF') and head[8:12] == b'WAVE'
        or head.startswith(b'OggS')
        or b'ftyp' in head[:16]
        or len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        or head.startswith(b'ADIF')
    )


def is_audio_upload(filename: str, content_type: str, data: bytes) -> bool:
    suffix = Path(filename or '').suffix.lower()
    return content_type in AUDIO_CONTENT_TYPES or suffix in AUDIO_EXTENSIONS or looks_like_audio(data)


def create_image(model: str, input_payload: dict, size: str, openid: str) -> dict:
    logger.info('ai_create_image_start openid=%s model=%s size=%s input_keys=%s', openid, model, size, ','.join(input_payload.keys()))
    try:
        task_id = ai_service.start_image_task(model, input_payload, size)
        task_result = ai_service.poll_task(task_id)
    except ai_service.DashScopeError as exc:
        if not ai_service.is_async_unsupported(exc):
            logger.warning('ai_create_image_async_failed openid=%s model=%s code=%s message=%s', openid, model, exc.code, exc.message)
            raise
        logger.info('ai_create_image_fallback_sync openid=%s model=%s reason=%s', openid, model, exc.message)
        task_result = ai_service.generate_image_sync(model, input_payload, size)
    image_url = ai_service.result_image_url(task_result)
    result = upload_generated_image(image_url, openid)
    logger.info('ai_create_image_success openid=%s model=%s object_key=%s', openid, model, result.get('objectKey'))
    return result


def create_image_auto(capability: str, requested_model: str | None, input_payload: dict, size: str, openid: str) -> tuple[dict, str]:
    candidates = ordered_candidates(capability, requested_model, vision_models(capability))
    last_error: ai_service.DashScopeError | None = None
    logger.info(
        'ai_create_image_auto_start openid=%s capability=%s requested=%s candidates=%s',
        openid,
        capability,
        requested_model or AUTO_MODEL_KEY,
        candidates,
    )
    for candidate in candidates:
        try:
            result = create_image(candidate, input_payload, size, openid)
            _MODEL_CACHE[capability] = candidate
            logger.info('ai_create_image_auto_success openid=%s capability=%s model=%s', openid, capability, candidate)
            return result, candidate
        except ai_service.DashScopeFreeQuotaExhausted as exc:
            last_error = exc
            mark_model_exhausted(capability, candidate)
            logger.warning('ai_create_image_auto_quota_exhausted openid=%s capability=%s model=%s', openid, capability, candidate)
        except ai_service.DashScopeError as exc:
            last_error = exc
            logger.warning(
                'ai_create_image_auto_candidate_failed openid=%s capability=%s model=%s code=%s message=%s',
                openid,
                capability,
                candidate,
                exc.code,
                exc.message,
            )
    if last_error:
        raise last_error
    raise ai_service.DashScopeError('No available image model')


def chat_completion_auto(requested_model: str | None, messages: list[dict[str, str]]) -> dict:
    capability = 'chat'
    candidates = ordered_candidates(capability, requested_model, settings.llm_model_list())
    last_error: ai_service.DashScopeError | None = None
    logger.info('ai_chat_auto_start requested=%s candidates=%s', requested_model or AUTO_MODEL_KEY, candidates)
    for candidate in candidates:
        try:
            data = ai_service.chat_completion(messages, candidate)
            _MODEL_CACHE[capability] = candidate
            logger.info('ai_chat_auto_success model=%s', candidate)
            return data
        except ai_service.DashScopeFreeQuotaExhausted as exc:
            last_error = exc
            mark_model_exhausted(capability, candidate)
            logger.warning('ai_chat_auto_quota_exhausted model=%s', candidate)
        except ai_service.DashScopeError as exc:
            last_error = exc
            logger.warning('ai_chat_auto_candidate_failed model=%s code=%s message=%s', candidate, exc.code, exc.message)
    if last_error:
        raise last_error
    raise ai_service.DashScopeError('No available chat model')


def transcribe_audio_auto(requested_model: str | None, file_url: str) -> tuple[str, str]:
    capability = 'transcription'
    candidates = ordered_candidates(capability, requested_model, settings.audio_model_list())
    last_error: ai_service.DashScopeError | None = None
    logger.info('ai_transcribe_auto_start requested=%s candidates=%s file_host=%s', requested_model or AUTO_MODEL_KEY, candidates, urlparse(file_url).netloc)
    for candidate in candidates:
        try:
            task_id = ai_service.start_transcription_task(file_url, candidate)
            text = ai_service.transcription_text(ai_service.poll_task(task_id, timeout_seconds=180))
            _MODEL_CACHE[capability] = candidate
            logger.info('ai_transcribe_auto_success model=%s chars=%s', candidate, len(text))
            return text, candidate
        except ai_service.DashScopeFreeQuotaExhausted as exc:
            last_error = exc
            mark_model_exhausted(capability, candidate)
            logger.warning('ai_transcribe_auto_quota_exhausted model=%s', candidate)
        except ai_service.DashScopeError as exc:
            last_error = exc
            logger.warning('ai_transcribe_auto_candidate_failed model=%s code=%s message=%s', candidate, exc.code, exc.message)
    if last_error:
        raise last_error
    raise ai_service.DashScopeError('No available transcription model')


def raise_ai_error(exc: ai_service.DashScopeError):
    logger.warning('ai_provider_error code=%s message=%s', exc.code, exc.message)
    if isinstance(exc, ai_service.DashScopeFreeQuotaExhausted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                'code': 'AI_FREE_QUOTA_EXHAUSTED',
                'providerCode': exc.code,
                'message': exc.message,
            },
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            'code': 'AI_PROVIDER_ERROR',
            'providerCode': exc.code,
            'message': str(exc),
        },
    ) from exc


@router.get('/models')
def ai_models(user=Depends(require_module('ai_suite'))):
    text_to_image_models = vision_models('textToImage')
    image_to_image_models = vision_models('imageToImage')
    logger.info(
        'ai_models_resolved openid=%s llm=%s vision=%s audio=%s',
        user['openid'],
        settings.llm_model_list(),
        text_to_image_models,
        settings.audio_model_list(),
    )
    return {
        'code': 200,
        'success': True,
        'data': {
            'freeQuotaNote': '仅接入阿里百炼免费额度模型，免费额度用完即止。',
            'models': {
                'chat': {
                    'default': AUTO_MODEL_KEY if settings.llm_model_list() else '',
                    'options': auto_option_items(settings.llm_model_list()) if settings.llm_model_list() else [],
                },
                'textToImage': {
                    'default': AUTO_MODEL_KEY if text_to_image_models else '',
                    'options': auto_option_items(text_to_image_models) if text_to_image_models else [],
                },
                'imageToImage': {
                    'default': AUTO_MODEL_KEY if image_to_image_models else '',
                    'options': auto_option_items(image_to_image_models) if image_to_image_models else [],
                },
                'transcription': {
                    'default': AUTO_MODEL_KEY if settings.audio_model_list() else '',
                    'options': auto_option_items(settings.audio_model_list()) if settings.audio_model_list() else [],
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
    requested_model = payload.model or AUTO_MODEL_KEY
    messages = [
        {
            'role': 'system',
            'content': (
                '你是一个个人工具集小程序中的通用AI助手。'
                '“1号垃圾桶”只是产品名称，不代表垃圾分类、垃圾回收或环保查询服务。'
                '除非用户明确询问相关主题，否则不要主动联想到垃圾分类。'
                '回答要简洁、准确、可操作。'
            ),
        },
        *normalize_chat_history(payload.history),
        {'role': 'user', 'content': message},
    ]
    try:
        data = chat_completion_auto(requested_model, messages)
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
    requested_model = payload.model or AUTO_MODEL_KEY
    logger.info('ai_text_to_image_request openid=%s model=%s prompt_len=%s size=%s', user['openid'], requested_model, len(prompt), payload.size or settings.ai_image_size)
    try:
        result, used_model = create_image_auto('textToImage', requested_model, {'prompt': prompt}, payload.size or settings.ai_image_size, user['openid'])
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
            'model': used_model,
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
    logger.info('ai_image_to_image_upload_start openid=%s filename=%s content_type=%s model=%s prompt_len=%s', user['openid'], file.filename, content_type, model, len(prompt))
    if content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    data = await file.read(settings.max_upload_size_bytes + 1)
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Image is too large')
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    check_image_security(data, file.filename or 'input.png', content_type)
    source = storage_service.upload_bytes(data, safe_filename('ai-source', content_type), content_type, AI_IMAGE_DIR)
    logger.info(
        'ai_image_to_image_source_uploaded openid=%s model=%s content_type=%s bytes=%s object_key=%s download_host=%s',
        user['openid'],
        model or AUTO_MODEL_KEY,
        content_type,
        len(data),
        source.get('objectKey'),
        urlparse(source.get('downloadUrl', '')).netloc,
    )
    image_input = {
        'prompt': prompt,
        'image_url': source['downloadUrl'],
        'base_image_url': source['downloadUrl'],
    }
    try:
        result, used_model = create_image_auto(
            'imageToImage',
            model or AUTO_MODEL_KEY,
            image_input,
            size or settings.ai_image_size,
            user['openid'],
        )
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
            'model': used_model,
        },
    }


@router.post('/transcribe')
async def transcribe_audio(
    file: UploadFile = File(...),
    model: str = Form(default=''),
    user=Depends(require_module('ai_suite')),
):
    content_type = file.content_type or 'application/octet-stream'
    logger.info('ai_transcribe_upload_start openid=%s filename=%s content_type=%s model=%s', user['openid'], file.filename, content_type, model)
    data = await file.read(settings.max_upload_size_bytes + 1)
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Audio is too large')
    source_name = Path(file.filename or '').name or safe_filename('audio', content_type)
    if not is_audio_upload(source_name, content_type, data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only audio files are supported')
    source = storage_service.upload_bytes(data, source_name, content_type, AI_AUDIO_DIR)
    logger.info(
        'ai_transcribe_source_uploaded openid=%s model=%s content_type=%s bytes=%s object_key=%s download_host=%s',
        user['openid'],
        model or AUTO_MODEL_KEY,
        content_type,
        len(data),
        source.get('objectKey'),
        urlparse(source.get('downloadUrl', '')).netloc,
    )
    try:
        text, used_model = transcribe_audio_auto(model or AUTO_MODEL_KEY, source['downloadUrl'])
    except ai_service.DashScopeError as exc:
        record_event(user['openid'], 'ai_suite', 'transcribe', False, str(exc))
        raise_ai_error(exc)
    record_event(user['openid'], 'ai_suite', 'transcribe', True, metadata={'size': len(data)})
    return {
        'code': 200,
        'success': True,
        'data': {
            'text': text,
            'model': used_model,
        },
    }
