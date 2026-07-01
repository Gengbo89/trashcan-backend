import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status

from src.services.analytics import record_event
from src.services.auth import get_conn, get_current_user, parse_token, row_to_user
from src.services.messages import (
    MessagePayload,
    ReadPayload,
    SubscribePayload,
    create_message,
    get_messages,
    get_subscription,
    list_conversations,
    mark_read,
    send_subscribe_message,
    set_subscription,
)

router = APIRouter()


class MessageConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}
        self.roles: dict[str, str] = {}

    async def connect(self, websocket: WebSocket, user: dict):
        await websocket.accept()
        openid = user['openid']
        self.connections.setdefault(openid, set()).add(websocket)
        self.roles[openid] = user['role']

    def disconnect(self, websocket: WebSocket, user: dict):
        openid = user['openid']
        sockets = self.connections.get(openid)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self.connections.pop(openid, None)
            self.roles.pop(openid, None)

    def is_online(self, openid: str) -> bool:
        return bool(self.connections.get(openid))

    async def send_to_user(self, openid: str, payload: dict) -> bool:
        sockets = list(self.connections.get(openid, set()))
        if not sockets:
            return False
        sent = False
        for socket in sockets:
            try:
                await socket.send_text(json.dumps(payload, ensure_ascii=False))
                sent = True
            except Exception:
                self.connections.get(openid, set()).discard(socket)
        return sent

    async def send_to_admins(self, payload: dict) -> bool:
        sent = False
        for openid, role in list(self.roles.items()):
            if role == 'admin':
                sent = await self.send_to_user(openid, payload) or sent
        return sent


manager = MessageConnectionManager()


def user_from_ws_token(token: str) -> dict | None:
    try:
        payload = parse_token(token)
        with get_conn() as conn:
            row = conn.execute('SELECT * FROM users WHERE openid = %s', (payload['openid'],)).fetchone()
        return row_to_user(row) if row else None
    except Exception:
        return None


@router.get('/conversations')
def conversations(user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'conversations': list_conversations(user)}}


@router.get('/messages')
def messages(targetOpenid: str | None = Query(default=None), user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'messages': get_messages(user, targetOpenid)}}


@router.post('/messages')
async def send_message(payload: MessagePayload, user=Depends(get_current_user)):
    message = create_message(user, payload.content, payload.targetOpenid)
    record_event(user['openid'], 'message', 'send', True, metadata={'senderRole': user['role']})
    event_payload = {'type': 'message', 'data': {'message': message}}
    if user['role'] == 'admin':
        delivered = await manager.send_to_user(message['userOpenid'], event_payload)
        if not delivered:
            send_subscribe_message(message['userOpenid'], message['content'])
    else:
        await manager.send_to_admins(event_payload)
    return {'code': 200, 'success': True, 'data': {'message': message}}


@router.post('/read')
def read_messages(payload: ReadPayload, user=Depends(get_current_user)):
    mark_read(user, payload.targetOpenid)
    return {'code': 200, 'success': True, 'data': {}}


@router.get('/subscription')
def subscription(user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'subscription': get_subscription(user['openid'])}}


@router.post('/subscription')
def update_subscription(payload: SubscribePayload, user=Depends(get_current_user)):
    return {
        'code': 200,
        'success': True,
        'data': {'subscription': set_subscription(user['openid'], payload.enabled)},
    }


@router.websocket('/ws')
async def message_ws(websocket: WebSocket, token: str = Query(default='')):
    user = user_from_ws_token(token)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect(websocket, user)
    try:
        while True:
            raw = await websocket.receive_text()
            if raw == 'ping':
                await websocket.send_text(json.dumps({'type': 'pong'}, ensure_ascii=False))
    except WebSocketDisconnect:
        manager.disconnect(websocket, user)
    except Exception:
        manager.disconnect(websocket, user)
