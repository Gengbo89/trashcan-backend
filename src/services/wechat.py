import json
import urllib.parse
import urllib.request

from src.config import settings


def get_wechat_access_token() -> str:
    if not settings.wechat_appid or not settings.wechat_secret:
        return ''
    query = urllib.parse.urlencode(
        {
            'grant_type': 'client_credential',
            'appid': settings.wechat_appid,
            'secret': settings.wechat_secret,
        }
    )
    with urllib.request.urlopen(f'https://api.weixin.qq.com/cgi-bin/token?{query}', timeout=8) as response:
        data = json.loads(response.read().decode('utf-8'))
    return data.get('access_token', '')
