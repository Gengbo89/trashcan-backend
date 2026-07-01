import json
import time
from typing import Any

from pydantic import BaseModel, Field

from src.services.auth import MODULES, get_conn, list_permission_groups


class EventPayload(BaseModel):
    module: str
    action: str
    success: bool = True
    message: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)


RANGE_SECONDS = {
    '1d': 24 * 3600,
    '7d': 7 * 24 * 3600,
    '30d': 30 * 24 * 3600,
}


def init_analytics_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS app_events (
                id BIGSERIAL PRIMARY KEY,
                openid TEXT REFERENCES users(openid) ON DELETE SET NULL,
                module TEXT NOT NULL,
                action TEXT NOT NULL,
                success BOOLEAN NOT NULL DEFAULT TRUE,
                message TEXT DEFAULT '',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at BIGINT NOT NULL
            )"""
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_app_events_created_at ON app_events(created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_app_events_module_created_at ON app_events(module, created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_app_events_success_created_at ON app_events(success, created_at DESC)')


def record_event(
    openid: str | None,
    module: str,
    action: str,
    success: bool = True,
    message: str = '',
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO app_events(openid, module, action, success, message, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
                (openid, module, action, success, message[:500], json.dumps(metadata or {}, ensure_ascii=False), int(time.time())),
            )
    except Exception:
        return


def range_start(range_key: str) -> int:
    return int(time.time()) - RANGE_SECONDS.get(range_key, RANGE_SECONDS['7d'])


def admin_dashboard(range_key: str = '7d') -> dict[str, Any]:
    start = range_start(range_key)
    today_start = int(time.time()) - 24 * 3600
    with get_conn() as conn:
        totals = conn.execute(
            """SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE created_at >= %s) AS new_users,
                (SELECT COUNT(DISTINCT openid) FROM app_events WHERE created_at >= %s AND openid IS NOT NULL) AS active_users,
                (SELECT COUNT(*) FROM app_events WHERE created_at >= %s AND module <> 'auth') AS tool_calls,
                (SELECT COUNT(*) FROM app_events WHERE created_at >= %s AND success = FALSE) AS failures,
                (SELECT COUNT(*) FROM user_messages WHERE sender_role = 'user' AND read_by_admin = FALSE) AS unread_messages
            """,
            (today_start, today_start, today_start, today_start),
        ).fetchone()

        tool_rows = conn.execute(
            """SELECT module,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE success = FALSE) AS failures
            FROM app_events
            WHERE created_at >= %s AND module <> 'auth'
            GROUP BY module
            ORDER BY total DESC""",
            (start,),
        ).fetchall()

        error_rows = conn.execute(
            """SELECT e.id, e.openid, e.module, e.action, e.message, e.created_at, u.nickname, u.avatar_url
            FROM app_events e
            LEFT JOIN users u ON u.openid = e.openid
            WHERE e.success = FALSE
            ORDER BY e.created_at DESC
            LIMIT 20"""
        ).fetchall()

        recent_users = conn.execute(
            """SELECT openid, nickname, avatar_url, role, permission_group, updated_at
            FROM users
            ORDER BY updated_at DESC
            LIMIT 10"""
        ).fetchall()

        pending_messages = conn.execute(
            """SELECT m.user_openid, u.nickname, u.avatar_url, COUNT(*) AS unread_count, MAX(m.created_at) AS last_time
            FROM user_messages m
            JOIN users u ON u.openid = m.user_openid
            WHERE m.sender_role = 'user' AND m.read_by_admin = FALSE
            GROUP BY m.user_openid, u.nickname, u.avatar_url
            ORDER BY last_time DESC
            LIMIT 5"""
        ).fetchall()

    modules = {**MODULES, 'auth': '登录认证'}
    max_total = max([int(row['total']) for row in tool_rows] or [1])
    return {
        'range': range_key,
        'overview': {
            'totalUsers': int(totals['total_users'] or 0),
            'newUsers': int(totals['new_users'] or 0),
            'activeUsers': int(totals['active_users'] or 0),
            'toolCalls': int(totals['tool_calls'] or 0),
            'failures': int(totals['failures'] or 0),
            'unreadMessages': int(totals['unread_messages'] or 0),
        },
        'toolUsage': [
            {
                'module': row['module'],
                'name': modules.get(row['module'], row['module']),
                'total': int(row['total']),
                'failures': int(row['failures'] or 0),
                'percent': round(int(row['total']) / max_total * 100),
            }
            for row in tool_rows
        ],
        'permissionGroups': list_permission_groups(),
        'recentErrors': [
            {
                'id': row['id'],
                'openid': row['openid'] or '',
                'nickName': row['nickname'] or '未知用户',
                'avatarUrl': row['avatar_url'] or '',
                'module': modules.get(row['module'], row['module']),
                'action': row['action'],
                'message': row['message'] or '',
                'createdAt': row['created_at'],
            }
            for row in error_rows
        ],
        'recentUsers': [
            {
                'openid': row['openid'],
                'nickName': row['nickname'] or '微信用户',
                'avatarUrl': row['avatar_url'] or '',
                'role': row['role'],
                'permissionGroup': row['permission_group'] or 'T2',
                'updatedAt': row['updated_at'],
            }
            for row in recent_users
        ],
        'pendingMessages': [
            {
                'openid': row['user_openid'],
                'nickName': row['nickname'] or '微信用户',
                'avatarUrl': row['avatar_url'] or '',
                'unreadCount': int(row['unread_count'] or 0),
                'lastTime': row['last_time'],
            }
            for row in pending_messages
        ],
    }
