import json
import time
import urllib.request
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel

from src.config import settings
from src.services.auth import get_conn
from src.services.security import check_text_security
from src.services.wechat import get_wechat_access_token


class MessagePayload(BaseModel):
    content: str
    targetOpenid: str | None = None


class ReadPayload(BaseModel):
    targetOpenid: str | None = None


class SubscribePayload(BaseModel):
    enabled: bool = True


def init_message_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_messages (
                id BIGSERIAL PRIMARY KEY,
                user_openid TEXT NOT NULL REFERENCES users(openid) ON DELETE CASCADE,
                sender_openid TEXT NOT NULL REFERENCES users(openid) ON DELETE CASCADE,
                sender_role TEXT NOT NULL,
                content TEXT NOT NULL,
                read_by_user BOOLEAN NOT NULL DEFAULT FALSE,
                read_by_admin BOOLEAN NOT NULL DEFAULT FALSE,
                created_at BIGINT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS message_subscriptions (
                openid TEXT PRIMARY KEY REFERENCES users(openid) ON DELETE CASCADE,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at BIGINT NOT NULL
            )"""
        )


def row_to_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'userOpenid': row['user_openid'],
        'senderOpenid': row['sender_openid'],
        'senderRole': row['sender_role'],
        'content': row['content'],
        'readByUser': bool(row['read_by_user']),
        'readByAdmin': bool(row['read_by_admin']),
        'createdAt': row['created_at'],
    }


def list_conversations(user: dict[str, Any]) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if user['role'] == 'admin':
            rows = conn.execute(
                """SELECT DISTINCT ON (m.user_openid)
                    m.user_openid,
                    u.nickname,
                    u.avatar_url,
                    u.permission_group,
                    u.last_ip,
                    u.last_ip_location,
                    m.content,
                    m.created_at,
                    (
                        SELECT COUNT(*) FROM user_messages unread
                        WHERE unread.user_openid = m.user_openid
                        AND unread.sender_role = 'user'
                        AND unread.read_by_admin = FALSE
                    ) AS unread_count
                FROM user_messages m
                JOIN users u ON u.openid = m.user_openid
                ORDER BY m.user_openid, m.created_at DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT
                    %s AS user_openid,
                    '管理员' AS nickname,
                    '' AS avatar_url,
                    '' AS permission_group,
                    '' AS last_ip,
                    '' AS last_ip_location,
                    COALESCE((SELECT content FROM user_messages WHERE user_openid = %s ORDER BY created_at DESC LIMIT 1), '') AS content,
                    COALESCE((SELECT created_at FROM user_messages WHERE user_openid = %s ORDER BY created_at DESC LIMIT 1), 0) AS created_at,
                    (SELECT COUNT(*) FROM user_messages WHERE user_openid = %s AND sender_role = 'admin' AND read_by_user = FALSE) AS unread_count
                """,
                (user['openid'], user['openid'], user['openid'], user['openid']),
            ).fetchall()
    return [
        {
            'userOpenid': row['user_openid'],
            'name': row['nickname'] or '微信用户',
            'avatarUrl': row['avatar_url'] or '',
            'permissionGroup': row.get('permission_group') or '',
            'lastIp': row.get('last_ip') or '',
            'lastIpLocation': row.get('last_ip_location') or '',
            'lastContent': row['content'] or '暂无消息',
            'lastTime': row['created_at'],
            'unreadCount': int(row['unread_count'] or 0),
        }
        for row in rows
    ]


def assert_target(user: dict[str, Any], target_openid: str | None) -> str:
    if user['role'] == 'admin':
        if not target_openid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='targetOpenid required')
        return target_openid
    return user['openid']


def get_messages(user: dict[str, Any], target_openid: str | None = None) -> list[dict[str, Any]]:
    user_openid = assert_target(user, target_openid)
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM user_messages WHERE user_openid = %s ORDER BY created_at ASC, id ASC',
            (user_openid,),
        ).fetchall()
    return [row_to_message(row) for row in rows]


def mark_read(user: dict[str, Any], target_openid: str | None = None) -> None:
    user_openid = assert_target(user, target_openid)
    with get_conn() as conn:
        if user['role'] == 'admin':
            conn.execute('UPDATE user_messages SET read_by_admin = TRUE WHERE user_openid = %s', (user_openid,))
        else:
            conn.execute('UPDATE user_messages SET read_by_user = TRUE WHERE user_openid = %s', (user_openid,))


def create_message(user: dict[str, Any], content: str, target_openid: str | None = None) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='content required')
    if len(content) > 1000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='content too long')
    check_text_security(content, user['openid'])
    user_openid = assert_target(user, target_openid)
    now = int(time.time())
    sender_role = 'admin' if user['role'] == 'admin' else 'user'
    with get_conn() as conn:
        target = conn.execute('SELECT openid FROM users WHERE openid = %s', (user_openid,)).fetchone()
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
        row = conn.execute(
            """INSERT INTO user_messages(user_openid, sender_openid, sender_role, content, read_by_user, read_by_admin, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *""",
            (user_openid, user['openid'], sender_role, content, sender_role == 'user', sender_role == 'admin', now),
        ).fetchone()
    return row_to_message(row)


def set_subscription(openid: str, enabled: bool) -> dict[str, Any]:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO message_subscriptions(openid, enabled, updated_at) VALUES (%s, %s, %s) ON CONFLICT(openid) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = EXCLUDED.updated_at',
            (openid, enabled, now),
        )
    return {'enabled': enabled, 'templateId': settings.wechat_message_template_id}


def get_subscription(openid: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute('SELECT enabled FROM message_subscriptions WHERE openid = %s', (openid,)).fetchone()
    return {
        'enabled': bool(row and row['enabled']),
        'templateId': settings.wechat_message_template_id,
    }


def count_user_unread_messages(openid: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total
            FROM user_messages
            WHERE user_openid = %s
            AND sender_role = 'admin'
            AND read_by_user = FALSE""",
            (openid,),
        ).fetchone()
    return int(row['total'] or 0) if row else 0


def send_subscribe_message(openid: str, content: str) -> None:
    if not settings.wechat_message_template_id:
        return
    subscription = get_subscription(openid)
    if not subscription['enabled']:
        return
    access_token = get_wechat_access_token()
    if not access_token:
        return
    unread_count = max(count_user_unread_messages(openid), 1)
    payload = {
        'touser': openid,
        'template_id': settings.wechat_message_template_id,
        'page': 'pages/message/index',
        'data': {
            'time2': {'value': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())},
            'number1': {'value': unread_count},
            'thing3': {'value': '您有新的未读消息，请注意查收'},
        },
    }
    req = urllib.request.Request(
        f'https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={access_token}',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        return
