from fastapi import APIRouter, Depends, File, Query, UploadFile, HTTPException, status
from fastapi.responses import RedirectResponse

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
    get_default_permissions,
    list_permission_groups,
    list_users,
    require_admin,
    set_group_permissions,
    set_user_group,
    update_profile,
)
from src.services.security import check_image_security, looks_like_image
from src.services.storage import storage_service

router = APIRouter()
AVATAR_DIR = 'avatars'
AVATAR_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp', 'application/octet-stream'}


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


@router.get('/public-modules')
def public_modules():
    return {
        'code': 200,
        'success': True,
        'data': {
            'modules': MODULES,
            'permissions': get_default_permissions(),
            'permissionGroup': 'T2',
            'permissionGroupName': 'T2 默认权限',
        },
    }


@router.patch('/me/profile')
def update_me_profile(payload: ProfilePayload, user=Depends(get_current_user)):
    updated_user = update_profile(user['openid'], payload.nickName or '', payload.avatarUrl or '')
    return {
        'code': 200,
        'success': True,
        'data': {'user': updated_user, 'modules': MODULES, 'permissionGroups': list_permission_groups()},
    }


@router.post('/me/avatar')
async def upload_me_avatar(file: UploadFile = File(...), user=Depends(get_current_user)):
    content_type = file.content_type or 'application/octet-stream'
    if content_type not in AVATAR_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    data = await file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='头像不能超过2M')
    if not looks_like_image(data):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only image files are supported')
    check_image_security(data, file.filename or 'avatar.jpg', content_type)
    result = storage_service.upload_bytes(data, f'{user["openid"]}-avatar.jpg', content_type, AVATAR_DIR)
    return {
        'code': 200,
        'success': True,
        'data': {
            'avatarUrl': result['downloadUrl'],
            'objectKey': result['objectKey'],
        },
    }


@router.get('/avatar')
def avatar(key: str = Query(...)):
    if not key.startswith(f'{AVATAR_DIR}/') or '..' in key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid avatar key')
    return RedirectResponse(storage_service.presign_object(key))


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
