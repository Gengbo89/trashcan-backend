import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from src.config import settings

MODULES = {
    'file_transfer': '文件中转',
    'image_crop': '图片裁剪',
    'document_convert': '文档转换',
    'qrcode': '二维码工具',
    'text_helper': '文本助手',
    'data_dashboard': '数据看板',
}
DEFAULT_ENABLED_MODULES = {'file_transfer'}
TOKEN_TTL_SECONDS = 30 * 24 * 3600


class LoginPayload(BaseModel):
    code: str
    nickName: str | None = None
    avatarUrl: str | None = None


class PermissionPayload(BaseModel):
    permissions: dict[str, bool]


class GlobalPermissionPayload(BaseModel):
    globalFirst: bool
    permissions: dict[str, bool]


class ProfilePayload(BaseModel):
    nickName: str | None = None
    avatarUrl: str | None = None


@contextmanager
def get_conn():
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                openid TEXT PRIMARY KEY,
                nickname TEXT DEFAULT '',
                avatar_url TEXT DEFAULT '',
                role TEXT DEFAULT 'user',
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS module_permissions (
                openid TEXT NOT NULL REFERENCES users(openid) ON DELETE CASCADE,
                module TEXT NOT NULL,
                enabled BOOLEAN NOT NULL,
                PRIMARY KEY(openid, module)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS global_permission_settings (
                id BOOLEAN PRIMARY KEY DEFAULT TRUE,
                global_first BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at BIGINT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS global_module_permissions (
                module TEXT PRIMARY KEY,
                enabled BOOLEAN NOT NULL
            )"""
        )
        now = int(time.time())
        conn.execute(
            'INSERT INTO global_permission_settings(id, global_first, updated_at) VALUES (TRUE, FALSE, %s) ON CONFLICT(id) DO NOTHING',
            (now,),
        )
        for module in MODULES:
            conn.execute(
                'INSERT INTO global_module_permissions(module, enabled) VALUES (%s, %s) ON CONFLICT(module) DO NOTHING',
                (module, module in DEFAULT_ENABLED_MODULES),
            )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def sign(data: str) -> str:
    return b64url(hmac.new(settings.jwt_secret.encode('utf-8'), data.encode('utf-8'), hashlib.sha256).digest())


def create_token(openid: str, role: str) -> str:
    header = b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode('utf-8'))
    payload = b64url(
        json.dumps(
            {'openid': openid, 'role': role, 'exp': int(time.time()) + TOKEN_TTL_SECONDS},
            separators=(',', ':'),
        ).encode('utf-8')
    )
    unsigned = f'{header}.{payload}'
    return f'{unsigned}.{sign(unsigned)}'


def parse_token(token: str) -> dict[str, Any]:
    try:
        header, payload, signature = token.split('.')
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid token') from exc
    unsigned = f'{header}.{payload}'
    if not hmac.compare_digest(signature, sign(unsigned)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid token')
    padded = payload + '=' * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))
    if data.get('exp', 0) < int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Token expired')
    return data


def fetch_wechat_openid(code: str) -> str:
    if not settings.wechat_appid or not settings.wechat_secret:
        if code.startswith('dev:'):
            return code[4:]
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Wechat credentials not configured')
    query = urllib.parse.urlencode(
        {
            'appid': settings.wechat_appid,
            'secret': settings.wechat_secret,
            'js_code': code,
            'grant_type': 'authorization_code',
        }
    )
    url = f'https://api.weixin.qq.com/sns/jscode2session?{query}'
    with urllib.request.urlopen(url, timeout=8) as response:
        data = json.loads(response.read().decode('utf-8'))
    if not data.get('openid'):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=data.get('errmsg') or 'Wechat login failed')
    return data['openid']


def get_global_permission_settings() -> dict[str, Any]:
    with get_conn() as conn:
        setting = conn.execute('SELECT global_first FROM global_permission_settings WHERE id = TRUE').fetchone()
        rows = conn.execute('SELECT module, enabled FROM global_module_permissions').fetchall()
    saved = {row['module']: bool(row['enabled']) for row in rows}
    return {
        'globalFirst': bool(setting and setting['global_first']),
        'permissions': {module: saved.get(module, module in DEFAULT_ENABLED_MODULES) for module in MODULES},
    }


def set_global_permission_settings(global_first: bool, permissions: dict[str, bool]) -> dict[str, Any]:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO global_permission_settings(id, global_first, updated_at) VALUES (TRUE, %s, %s) ON CONFLICT(id) DO UPDATE SET global_first = EXCLUDED.global_first, updated_at = EXCLUDED.updated_at',
            (global_first, now),
        )
        for module, enabled in permissions.items():
            if module not in MODULES:
                continue
            conn.execute(
                'INSERT INTO global_module_permissions(module, enabled) VALUES (%s, %s) ON CONFLICT(module) DO UPDATE SET enabled = EXCLUDED.enabled',
                (module, enabled),
            )
    return get_global_permission_settings()


def get_personal_permissions(openid: str) -> dict[str, bool]:
    with get_conn() as conn:
        rows = conn.execute('SELECT module, enabled FROM module_permissions WHERE openid = %s', (openid,)).fetchall()
    saved = {row['module']: bool(row['enabled']) for row in rows}
    return {module: saved.get(module, module in DEFAULT_ENABLED_MODULES) for module in MODULES}


def get_permissions(openid: str) -> dict[str, bool]:
    global_settings = get_global_permission_settings()
    if global_settings['globalFirst']:
        return global_settings['permissions']
    return get_personal_permissions(openid)


def row_to_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'openid': row['openid'],
        'nickName': row['nickname'],
        'avatarUrl': row['avatar_url'],
        'role': row['role'],
        'permissions': get_permissions(row['openid']),
        'personalPermissions': get_personal_permissions(row['openid']),
    }


def ensure_user(openid: str, nickname: str = '', avatar_url: str = '') -> dict[str, Any]:
    now = int(time.time())
    with get_conn() as conn:
        count = conn.execute('SELECT COUNT(*) AS count FROM users').fetchone()['count']
        role = 'admin' if openid in settings.admin_openid_set or count == 0 else 'user'
        existing = conn.execute('SELECT * FROM users WHERE openid = %s', (openid,)).fetchone()
        if existing:
            role = 'admin' if openid in settings.admin_openid_set else existing['role']
            conn.execute(
                "UPDATE users SET nickname = COALESCE(NULLIF(%s, ''), nickname), avatar_url = COALESCE(NULLIF(%s, ''), avatar_url), role = %s, updated_at = %s WHERE openid = %s",
                (nickname, avatar_url, role, now, openid),
            )
        else:
            conn.execute(
                'INSERT INTO users(openid, nickname, avatar_url, role, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)',
                (openid, nickname, avatar_url, role, now, now),
            )
            for module in MODULES:
                conn.execute(
                    'INSERT INTO module_permissions(openid, module, enabled) VALUES (%s, %s, %s) ON CONFLICT(openid, module) DO UPDATE SET enabled = EXCLUDED.enabled',
                    (openid, module, module in DEFAULT_ENABLED_MODULES),
                )
        user = conn.execute('SELECT * FROM users WHERE openid = %s', (openid,)).fetchone()
    return row_to_user(user)



def update_profile(openid: str, nickname: str = '', avatar_url: str = '') -> dict[str, Any]:
    now = int(time.time())
    with get_conn() as conn:
        user = conn.execute('SELECT * FROM users WHERE openid = %s', (openid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
        conn.execute(
            "UPDATE users SET nickname = COALESCE(NULLIF(%s, ''), nickname), avatar_url = COALESCE(NULLIF(%s, ''), avatar_url), updated_at = %s WHERE openid = %s",
            (nickname, avatar_url, now, openid),
        )
        updated = conn.execute('SELECT * FROM users WHERE openid = %s', (openid,)).fetchone()
    return row_to_user(updated)

def list_users(keyword: str = '') -> list[dict[str, Any]]:
    keyword = keyword.strip()
    with get_conn() as conn:
        if keyword:
            pattern = f'%{keyword}%'
            rows = conn.execute(
                'SELECT * FROM users WHERE nickname ILIKE %s OR openid ILIKE %s ORDER BY updated_at DESC',
                (pattern, pattern),
            ).fetchall()
        else:
            rows = conn.execute('SELECT * FROM users ORDER BY updated_at DESC').fetchall()
    return [row_to_user(row) for row in rows]


def set_permissions(openid: str, permissions: dict[str, bool]) -> dict[str, Any]:
    with get_conn() as conn:
        user = conn.execute('SELECT * FROM users WHERE openid = %s', (openid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
        for module, enabled in permissions.items():
            if module not in MODULES:
                continue
            conn.execute(
                'INSERT INTO module_permissions(openid, module, enabled) VALUES (%s, %s, %s) ON CONFLICT(openid, module) DO UPDATE SET enabled = EXCLUDED.enabled',
                (openid, module, enabled),
            )
    return ensure_user(openid)


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Login required')
    payload = parse_token(authorization.removeprefix('Bearer ').strip())
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM users WHERE openid = %s', (payload['openid'],)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='User not found')
    return row_to_user(row)


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user['role'] != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin required')
    return user


def require_module(module: str):
    def dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user['permissions'].get(module):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Permission denied')

    return dependency
