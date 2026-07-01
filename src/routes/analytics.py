from fastapi import APIRouter, Depends, Query

from src.services.analytics import EventPayload, admin_dashboard, record_event
from src.services.auth import get_current_user, require_module

router = APIRouter()


@router.post('/events')
def create_event(payload: EventPayload, user=Depends(get_current_user)):
    record_event(user['openid'], payload.module, payload.action, payload.success, payload.message, payload.metadata)
    return {'code': 200, 'success': True, 'data': {}}


@router.get('/dashboard')
def dashboard(range_key: str = Query(default='7d', alias='range'), _=Depends(require_module('data_dashboard'))):
    return {'code': 200, 'success': True, 'data': admin_dashboard(range_key)}


@router.get('/admin/dashboard')
def admin_dashboard_compat(range_key: str = Query(default='7d', alias='range'), _=Depends(require_module('data_dashboard'))):
    return {'code': 200, 'success': True, 'data': admin_dashboard(range_key)}
