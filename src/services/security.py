import json
import mimetypes
import urllib.request
from uuid import uuid4

from fastapi import HTTPException, status

from src.services.wechat import get_wechat_access_token

UNSAFE_ERRCODE = 87014
SECURITY_SCENE = 2


def looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(b'\xff\xd8\xff')
        or data.startswith(b'\x89PNG\r\n\x1a\n')
        or data.startswith(b'GIF87a')
        or data.startswith(b'GIF89a')
        or data.startswith(b'BM')
        or (data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WEBP')
    )


def raise_unsafe_content(message: str = '内容含违规信息') -> None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def read_wechat_response(response) -> dict:
    return json.loads(response.read().decode('utf-8'))


def assert_wechat_security_response(data: dict, unsafe_message: str = '内容含违规信息') -> None:
    if data.get('errcode') in (0, None):
        result = data.get('result') or {}
        if not result or result.get('suggest') == 'pass':
            return
    if data.get('errcode') == UNSAFE_ERRCODE or (data.get('result') or {}).get('suggest') in {'risky', 'review'}:
        raise_unsafe_content(unsafe_message)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='内容安全校验失败')


def check_text_security(content: str, openid: str = '') -> None:
    content = content.strip()
    if not content:
        return
    access_token = get_wechat_access_token()
    if not access_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='内容安全校验不可用')
    payload = {
        'content': content[:2500],
        'version': 2,
        'scene': SECURITY_SCENE,
        'openid': openid or 'system',
    }
    request = urllib.request.Request(
        f'https://api.weixin.qq.com/wxa/msg_sec_check?access_token={access_token}',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = read_wechat_response(response)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='内容安全校验失败') from exc
    assert_wechat_security_response(data, '内容含违规信息')


def check_image_security(data: bytes, filename: str = 'image.jpg', content_type: str = '') -> None:
    access_token = get_wechat_access_token()
    if not access_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='内容安全校验不可用')

    content_type = content_type or mimetypes.guess_type(filename)[0] or 'image/jpeg'
    boundary = f'----trashcan-security-{uuid4().hex}'
    header = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode('utf-8')
    body = header + data + f'\r\n--{boundary}--\r\n'.encode('utf-8')
    request = urllib.request.Request(
        f'https://api.weixin.qq.com/wxa/img_sec_check?access_token={access_token}',
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = read_wechat_response(response)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='内容安全校验失败') from exc
    assert_wechat_security_response(result, '图片含违规信息')


def check_image_url_security(media_url: str, openid: str = '') -> None:
    access_token = get_wechat_access_token()
    if not access_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='内容安全校验不可用')
    payload = {
        'media_url': media_url,
        'media_type': 2,
        'version': 2,
        'scene': SECURITY_SCENE,
        'openid': openid or 'system',
    }
    request = urllib.request.Request(
        f'https://api.weixin.qq.com/wxa/media_check_async?access_token={access_token}',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = read_wechat_response(response)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='内容安全校验失败') from exc
    assert_wechat_security_response(data, '图片含违规信息')
