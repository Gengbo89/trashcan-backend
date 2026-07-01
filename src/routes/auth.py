from fastapi import APIRouter, Depends, Query

from src.services.analytics import record_event
from src.services.auth import (
    GroupPermissionPayload,
    LoginPayload,
    MODULES,
    ProfilePayload,
    UserGroupPayload,
    create_token,
    ensure_user,
    fetch_wechat_openid,
    get_current_user,
    list_permission_groups,
    list_users,
    require_admin,
    set_group_permissions,
    set_user_group,
    update_profile,
)

router = APIRouter()


@router.post('/wechat-login')
def wechat_login(payload: LoginPayload):
    openid = fetch_wechat_openid(payload.code)
    user = ensure_user(openid, payload.nickName or '', payload.avatarUrl or '')
    record_event(user['openid'], 'auth', 'login', True)
    return {
        'code': 200,
        'success': True,
        'data': {
            'token': create_token(user['openid'], user['role']),
            'user': user,
            'modules': MODULES,
            'permissionGroups': list_permission_groups(),
        },
    }


@router.get('/me')
def me(user=Depends(get_current_user)):
    return {
        'code': 200,
        'success': True,
        'data': {'user': user, 'modules': MODULES, 'permissionGroups': list_permission_groups()},
    }


@router.patch('/me/profile')
def update_me_profile(payload: ProfilePayload, user=Depends(get_current_user)):
    updated_user = update_profile(user['openid'], payload.nickName or '', payload.avatarUrl or '')
    return {
        'code': 200,
        'success': True,
        'data': {'user': updated_user, 'modules': MODULES, 'permissionGroups': list_permission_groups()},
    }


@router.get('/modules')
def modules(user=Depends(get_current_user)):
    return {
        'code': 200,
        'success': True,
        'data': {
            'modules': MODULES,
            'permissions': user['permissions'],
            'permissionGroups': list_permission_groups(),
        },
    }


@router.get('/admin/users')
def admin_users(keyword: str = Query(default=''), _=Depends(require_admin)):
    return {
        'code': 200,
        'success': True,
        'data': {
            'users': list_users(keyword),
            'modules': MODULES,
            'permissionGroups': list_permission_groups(),
        },
    }


@router.put('/admin/permission-groups/{group_key}/permissions')
def admin_set_group_permissions(group_key: str, payload: GroupPermissionPayload, _=Depends(require_admin)):
    return {
        'code': 200,
        'success': True,
        'data': {
            'permissionGroup': set_group_permissions(group_key, payload.permissions),
            'permissionGroups': list_permission_groups(),
            'modules': MODULES,
        },
    }


@router.put('/admin/users/{openid}/group')
def admin_set_user_group(openid: str, payload: UserGroupPayload, _=Depends(require_admin)):
    return {'code': 200, 'success': True, 'data': {'user': set_user_group(openid, payload.groupKey), 'modules': MODULES}}
