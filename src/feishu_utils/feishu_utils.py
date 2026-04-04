import datetime
import re
import requests
import json
import os

app_id = os.getenv('APP_ID')
app_secret = os.getenv('APP_SECRET')

assert app_id and app_secret, 'app_id and app_secret is required'

def get_tenant_access_token():
    """
    获取飞书的tenant_access_token
    :return:
    """
    res = requests.post(url='https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal', json={"app_id": app_id, "app_secret": app_secret}).json()
    return res['app_access_token']

def get_headers(access_token):
    return {'Authorization': 'Bearer ' + access_token}

def reply_message(message_id, text, access_token=None):
    if access_token is None:
        access_token = get_tenant_access_token()
        
    url = 'https://open.feishu.cn/open-apis/im/v1/messages/{}/reply'.format(message_id)
    
    ret_data = {'text': text}
    
    body = {
        "msg_type": "text",
        "content": json.dumps(ret_data, ensure_ascii=False, indent=4),
        'uuid': str(datetime.datetime.now().timestamp())
    }
    res = requests.post(url, headers=get_headers(access_token), json=body).json()
    return res

def send_message(receive_id, text, access_token=None):
    if access_token is None:
        access_token = get_tenant_access_token()
        
    url = 'https://open.feishu.cn/open-apis/im/v1/messages'
    param = {'receive_id_type': 'chat_id'}
    
    ret_data = {'text':text}
    
    body = {
        'receive_id': receive_id,
        "msg_type": "text",
        "content": json.dumps(ret_data, ensure_ascii=False, indent=4),
        'uuid': str(datetime.datetime.now().timestamp())
    }
    res = requests.post(url, headers=get_headers(access_token), json=body, params=param).json()
    return res

def add_reaction(message_id, emoji_type, access_token=None):
    """给消息添加表情回应"""
    if access_token is None:
        access_token = get_tenant_access_token()

    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions'
    body = {"reaction_type": {"emoji_type": emoji_type}}
    res = requests.post(url, headers=get_headers(access_token), json=body).json()
    return res

def remove_reaction(message_id, reaction_id, access_token=None):
    """移除表情回应"""
    if access_token is None:
        access_token = get_tenant_access_token()

    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}'
    res = requests.delete(url, headers=get_headers(access_token)).json()
    return res


def download_message_resource(message_id, file_key, res_type="image", access_token=None):
    """
    下载消息中的资源文件（图片/文件）

    Args:
        message_id: 消息 ID
        file_key: 资源 key（image_key 或 file_key）
        res_type: 资源类型，"image" 或 "file"

    Returns:
        本地文件路径，失败返回 None
    """
    if access_token is None:
        access_token = get_tenant_access_token()

    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}'
    params = {'type': res_type}
    res = requests.get(url, headers=get_headers(access_token), params=params, stream=True)

    if res.status_code != 200:
        return None

    # 从 Content-Disposition 或 Content-Type 推断扩展名
    content_type = res.headers.get('Content-Type', '')
    ext_map = {
        'image/png': '.png',
        'image/jpeg': '.jpg',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'application/pdf': '.pdf',
    }
    ext = ext_map.get(content_type, '.bin')

    save_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'downloads')
    os.makedirs(save_dir, exist_ok=True)
    file_path = os.path.join(save_dir, f'{file_key}{ext}')

    with open(file_path, 'wb') as f:
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)

    return file_path


def get_department_member_list(department_id, access_token=None):
    if access_token is None:
        access_token = get_tenant_access_token()
        
    # 获取部门直属用户列表
    url = 'https://open.feishu.cn/open-apis/contact/v3/users/find_by_department'
    params = {'department_id': department_id}
    res = requests.get(url, headers=get_headers(access_token), params=params).json()
    if res['code'] !=0:
        raise Exception(f'get_department_member_list() get err res:{json.dumps(res)}')
    return res

def get_chats_member_list(chat_id, access_token=None):
    if access_token is None:
        access_token = get_tenant_access_token()
        
    # 先查看机器人是否在群里
    url = f'https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members/is_in_chat'
    res = requests.get(url, headers=get_headers(access_token)).json()
    if res['code'] !=0 or not res['data']['is_in_chat']:
        return {"data" : {"items": []}}
        # raise Exception(f'get_chats_member_list() get err res:{json.dumps(res)}')
    
    # 获取群成员列表
    url = f'https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members'
    res = requests.get(url, headers=get_headers(access_token)).json()
    
    if res['code'] !=0:
        raise Exception(f'get_chats_member_list() get err res:{json.dumps(res)}')
    return res