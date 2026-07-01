from fastapi import APIRouter, Depends, Query

from src.services.auth import get_current_user
from src.services.messages import (
    MessagePayload,
    ReadPayload,
    SubscribePayload,
    create_message,
    get_messages,
    get_subscription,
    list_conversations,
    mark_read,
    set_subscription,
)

router = APIRouter()


@router.get('/conversations')
def conversations(user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'conversations': list_conversations(user)}}


@router.get('/messages')
def messages(targetOpenid: str | None = Query(default=None), user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'messages': get_messages(user, targetOpenid)}}


@router.post('/messages')
def send_message(payload: MessagePayload, user=Depends(get_current_user)):
    message = create_message(user, payload.content, payload.targetOpenid)
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
