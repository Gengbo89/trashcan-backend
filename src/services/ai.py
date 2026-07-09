import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException, status

from src.config import settings


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


def dashscope_request(path_or_url: str, payload: dict[str, Any] | None = None, method: str = 'POST', extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith('http') else f'{settings.dashscope_base_url.rstrip("/")}{path_or_url}'
    headers = dashscope_json_headers()
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8') if method != 'GET' else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8'))
        except Exception:
            detail = {'message': exc.reason}
        try:
            raise_dashscope_error(detail)
        except DashScopeError as err:
            raise err from exc
    except Exception as exc:
        raise DashScopeError('DashScope request failed') from exc


def ensure_success(data: dict[str, Any]) -> dict[str, Any]:
    if data.get('code') and data.get('code') not in {'Success', '200'}:
        raise_dashscope_error(data)
    return data


def chat_completion(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
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
    return {'content': content, 'model': data.get('model') or model}


def start_image_task(model: str, input_payload: dict[str, Any], size: str) -> str:
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
    )
    task_id = ((data.get('output') or {}).get('task_id') or '').strip()
    if not task_id:
        raise_dashscope_error(data, 'Image task was not created')
    return task_id


def generate_image_sync(model: str, input_payload: dict[str, Any], size: str) -> dict[str, Any]:
    payload = {
        'model': model,
        'input': input_payload,
        'parameters': {
            'size': size or settings.ai_image_size,
            'n': 1,
        },
    }
    return ensure_success(dashscope_request('/api/v1/services/aigc/text2image/image-synthesis', payload))


def start_transcription_task(file_url: str, model: str) -> str:
    payload = {
        'model': model,
        'input': {'file_urls': [file_url]},
        'parameters': {'language_hints': ['zh', 'en']},
    }
    data = dashscope_request(
        '/api/v1/services/audio/asr/transcription',
        payload,
        extra_headers={'X-DashScope-Async': 'enable'},
    )
    task_id = ((data.get('output') or {}).get('task_id') or '').strip()
    if not task_id:
        raise_dashscope_error(data, 'Transcription task was not created')
    return task_id


def poll_task(task_id: str, timeout_seconds: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_data: dict[str, Any] = {}
    while time.time() < deadline:
        data = dashscope_request(f'/api/v1/tasks/{urllib.parse.quote(task_id)}', method='GET')
        last_data = ensure_success(data)
        output = data.get('output') or {}
        status_text = output.get('task_status')
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
        return results[0]['url']
    if output.get('url'):
        return output['url']
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
    request = urllib.request.Request(url, headers={'User-Agent': 'trashcan-backend/1.0'}, method='GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def download_binary(url: str, max_size: int) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={'User-Agent': 'trashcan-backend/1.0'}, method='GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        data = response.read(max_size + 1)
    if len(data) > max_size:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Generated file is too large')
    return data, content_type
