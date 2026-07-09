import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException, status

from src.config import settings

logger = logging.getLogger(__name__)


class DashScopeError(Exception):
    def __init__(self, message: str, code: str = ''):
        super().__init__(message)
        self.message = message
        self.code = code


class DashScopeFreeQuotaExhausted(DashScopeError):
    pass


def is_async_unsupported(exc: DashScopeError) -> bool:
    return 'does not support asynchronous calls' in exc.message.lower()


def raise_dashscope_error(detail: Any, fallback_message: str = 'DashScope request failed'):
    if not isinstance(detail, dict):
        raise DashScopeError(str(detail or fallback_message))
    error = detail.get('error') if isinstance(detail.get('error'), dict) else {}
    code = str(detail.get('code') or detail.get('error_code') or error.get('code') or '')
    message = str(detail.get('message') or detail.get('error_msg') or error.get('message') or code or fallback_message)
    logger.warning('dashscope_error code=%s message=%s detail=%s', code or '-', message, detail)
    if code == 'AllocationQuota.FreeTierOnly' or 'AllocationQuota.FreeTierOnly' in message:
        raise DashScopeFreeQuotaExhausted('阿里百炼免费额度已用尽，平台已自动停止服务。', code)
    raise DashScopeError(message, code)


def assert_api_key() -> str:
    if not settings.dashscope_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='AI service is not configured')
    return settings.dashscope_api_key


def dashscope_json_headers() -> dict[str, str]:
    return {
        'Authorization': f'Bearer {assert_api_key()}',
        'Content-Type': 'application/json',
    }


def dashscope_url(path_or_url: str, base_url: str | None = None) -> str:
    if path_or_url.startswith('http'):
        return path_or_url
    base = (base_url or settings.dashscope_service_url or settings.dashscope_base_url).rstrip('/')
    path = path_or_url if path_or_url.startswith('/') else f'/{path_or_url}'
    if base.endswith('/api/v1') and path.startswith('/api/v1/'):
        path = path[len('/api/v1'):]
    return f'{base}{path}'


def dashscope_request(
    path_or_url: str,
    payload: dict[str, Any] | None = None,
    method: str = 'POST',
    extra_headers: dict[str, str] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    url = dashscope_url(path_or_url, base_url)
    headers = dashscope_json_headers()
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8') if method != 'GET' else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    payload_keys = ','.join((payload or {}).keys())
    logger.info('dashscope_request method=%s url=%s payload_keys=%s async=%s', method, url, payload_keys, bool(extra_headers and extra_headers.get('X-DashScope-Async')))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode('utf-8')
            logger.info('dashscope_response status=%s url=%s bytes=%s', response.status, url, len(text))
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        try:
            error_text = exc.read().decode('utf-8')
            detail = json.loads(error_text)
        except Exception:
            detail = {'message': exc.reason}
            error_text = str(exc.reason)
        logger.warning('dashscope_http_error status=%s url=%s body=%s', exc.code, url, error_text[:2000])
        try:
            raise_dashscope_error(detail)
        except DashScopeError as err:
            raise err from exc
    except Exception as exc:
        logger.exception('dashscope_request_failed url=%s', url)
        raise DashScopeError('DashScope request failed') from exc


def ensure_success(data: dict[str, Any]) -> dict[str, Any]:
    if data.get('code') and data.get('code') not in {'Success', '200'}:
        raise_dashscope_error(data)
    return data


def chat_completion(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    logger.info('ai_chat_start model=%s messages=%s', model, len(messages))
    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.7,
    }
    data = dashscope_request(f'{settings.dashscope_compatible_url.rstrip("/")}/chat/completions', payload)
    choices = data.get('choices') or []
    if not choices:
        raise DashScopeError('AI response is empty')
    content = ((choices[0].get('message') or {}).get('content') or '').strip()
    if not content:
        raise DashScopeError('AI response is empty')
    logger.info('ai_chat_success model=%s chars=%s', data.get('model') or model, len(content))
    return {'content': content, 'model': data.get('model') or model}


def start_image_task(model: str, input_payload: dict[str, Any], size: str) -> str:
    logger.info('ai_image_async_start model=%s size=%s input_keys=%s', model, size, ','.join(input_payload.keys()))
    payload = {
        'model': model,
        'input': input_payload,
        'parameters': {
            'size': size or settings.ai_image_size,
            'n': 1,
        },
    }
    data = dashscope_request(
        '/api/v1/services/aigc/text2image/image-synthesis',
        payload,
        extra_headers={'X-DashScope-Async': 'enable'},
        base_url=settings.dashscope_service_url,
    )
    task_id = ((data.get('output') or {}).get('task_id') or '').strip()
    if not task_id:
        raise_dashscope_error(data, 'Image task was not created')
    logger.info('ai_image_async_created model=%s task_id=%s', model, task_id)
    return task_id


def generate_image_sync(model: str, input_payload: dict[str, Any], size: str) -> dict[str, Any]:
    logger.info('ai_image_sync_start model=%s size=%s input_keys=%s', model, size, ','.join(input_payload.keys()))
    payload = {
        'model': model,
        'input': input_payload,
        'parameters': {
            'size': size or settings.ai_image_size,
            'n': 1,
        },
    }
    data = ensure_success(dashscope_request('/api/v1/services/aigc/text2image/image-synthesis', payload, base_url=settings.dashscope_service_url))
    logger.info('ai_image_sync_success model=%s output_keys=%s', model, ','.join((data.get('output') or {}).keys()))
    return data


def start_transcription_task(file_url: str, model: str) -> str:
    logger.info('ai_transcription_async_start model=%s file_url_host=%s', model, urllib.parse.urlparse(file_url).netloc)
    payload = {
        'model': model,
        'input': {'file_urls': [file_url]},
        'parameters': {'language_hints': ['zh', 'en']},
    }
    data = dashscope_request(
        '/api/v1/services/audio/asr/transcription',
        payload,
        extra_headers={'X-DashScope-Async': 'enable'},
        base_url=settings.dashscope_service_url,
    )
    task_id = ((data.get('output') or {}).get('task_id') or '').strip()
    if not task_id:
        raise_dashscope_error(data, 'Transcription task was not created')
    logger.info('ai_transcription_async_created model=%s task_id=%s', model, task_id)
    return task_id


def poll_task(task_id: str, timeout_seconds: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_data: dict[str, Any] = {}
    while time.time() < deadline:
        data = dashscope_request(f'/api/v1/tasks/{urllib.parse.quote(task_id)}', method='GET', base_url=settings.dashscope_service_url)
        last_data = ensure_success(data)
        output = data.get('output') or {}
        status_text = output.get('task_status')
        logger.info('dashscope_task_status task_id=%s status=%s', task_id, status_text)
        if status_text == 'SUCCEEDED':
            return data
        if status_text in {'FAILED', 'CANCELED', 'UNKNOWN'}:
            raise DashScopeError(output.get('message') or f'Task {status_text.lower()}')
        time.sleep(2)
    raise DashScopeError((last_data.get('output') or {}).get('message') or 'Task timed out')


def result_image_url(task_result: dict[str, Any]) -> str:
    output = task_result.get('output') or {}
    results = output.get('results') or []
    if results and results[0].get('url'):
        logger.info('ai_image_result_url source=results host=%s', urllib.parse.urlparse(results[0]['url']).netloc)
        return results[0]['url']
    if output.get('url'):
        logger.info('ai_image_result_url source=output host=%s', urllib.parse.urlparse(output['url']).netloc)
        return output['url']
    logger.warning('ai_image_result_url_missing output=%s', output)
    raise DashScopeError('Generated image URL is empty')


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return '\n'.join(item for item in (extract_text(item) for item in value) if item)
    if isinstance(value, dict):
        for key in ('text', 'transcript', 'sentence', 'result'):
            if value.get(key):
                text = extract_text(value[key])
                if text:
                    return text
        return '\n'.join(item for item in (extract_text(item) for item in value.values()) if item)
    return ''


def transcription_text(task_result: dict[str, Any]) -> str:
    output = task_result.get('output') or {}
    results = output.get('results') or []
    for result in results:
        url = result.get('transcription_url') or result.get('url')
        if url:
            data = download_json(url)
            text = extract_text(data)
            if text:
                return text
        text = extract_text(result)
        if text:
            return text
    text = extract_text(output)
    if text:
        return text
    raise DashScopeError('Transcription result is empty')


def download_json(url: str) -> Any:
    logger.info('download_json_start host=%s path=%s', urllib.parse.urlparse(url).netloc, urllib.parse.urlparse(url).path)
    request = urllib.request.Request(url, headers={'User-Agent': 'trashcan-backend/1.0'}, method='GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode('utf-8')
        logger.info('download_json_success status=%s bytes=%s', response.status, len(text))
        return json.loads(text)


def download_binary(url: str, max_size: int) -> tuple[bytes, str]:
    logger.info('download_binary_start host=%s path=%s max_size=%s', urllib.parse.urlparse(url).netloc, urllib.parse.urlparse(url).path, max_size)
    request = urllib.request.Request(url, headers={'User-Agent': 'trashcan-backend/1.0'}, method='GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        data = response.read(max_size + 1)
        logger.info('download_binary_success status=%s content_type=%s bytes=%s', response.status, content_type, len(data))
    if len(data) > max_size:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Generated file is too large')
    return data, content_type
