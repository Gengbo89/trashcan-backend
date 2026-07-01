from fastapi import APIRouter, Depends

from src.services.auth import (
    LoginPayload,
    MODULES,
    PermissionPayload,
    ProfilePayload,
    create_token,
    ensure_user,
    fetch_wechat_openid,
    get_current_user,
    list_users,
    require_admin,
    set_permissions,
    update_profile,
)

router = APIRouter()


@router.post('/wechat-login')
def wechat_login(payload: LoginPayload):
    openid = fetch_wechat_openid(payload.code)
    user = ensure_user(openid, payload.nickName or '', payload.avatarUrl or '')
    return {
        'code': 200,
        'success': True,
        'data': {
            'token': create_token(user['openid'], user['role']),
            'user': user,
            'modules': MODULES,
        },
    }


@router.get('/me')
def me(user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'user': user, 'modules': MODULES}}


@router.patch('/me/profile')
def update_me_profile(payload: ProfilePayload, user=Depends(get_current_user)):
    updated_user = update_profile(user['openid'], payload.nickName or '', payload.avatarUrl or '')
    return {'code': 200, 'success': True, 'data': {'user': updated_user, 'modules': MODULES}}


@router.get('/modules')
def modules(user=Depends(get_current_user)):
    return {'code': 200, 'success': True, 'data': {'modules': MODULES, 'permissions': user['permissions']}}


@router.get('/admin/users')
def admin_users(_=Depends(require_admin)):
    return {'code': 200, 'success': True, 'data': {'users': list_users(), 'modules': MODULES}}


@router.put('/admin/users/{openid}/permissions')
def admin_set_permissions(openid: str, payload: PermissionPayload, _=Depends(require_admin)):
    return {'code': 200, 'success': True, 'data': {'user': set_permissions(openid, payload.permissions), 'modules': MODULES}}
