"""Small DeepSeek client for connection checks, search and grounded discussion."""
import json
import re
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError


class ConnectionError(RuntimeError):
    pass


def save_connection(path,key,model):
    """Update only our two settings, preserving the existing file and its ACL."""
    if not re.fullmatch(r'sk-[A-Za-z0-9_-]+',key):
        raise ConnectionError('密钥格式不完整，请重新粘贴。')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',model):
        raise ConnectionError('模型名称无效。')
    values={'HEATPLAN_API_KEY':key,'HEATPLAN_MODEL':model}
    text=path.read_text(encoding='utf-8') if path.exists() else ''
    for name,value in values.items():
        line=f"{name}='{value}'"
        pattern=r'(?m)^\s*(?:export\s+)?'+name+r'\s*=.*$'
        text=re.sub(pattern,lambda _:line,text) if re.search(pattern,text) else text.rstrip()+'\n'+line+'\n'
    # In-place write avoids Windows denying replacement of an existing .env.
    with path.open('w',encoding='utf-8',newline='\n') as config:
        config.write(text)


def api_json(path, key, payload=None, timeout=45):
    if not str(key or '').strip():
        raise ConnectionError('请先粘贴 DeepSeek 密钥并点击“应用并检查连接”。')
    req=Request('https://api.deepseek.com/'+path.lstrip('/'),
        headers={'Authorization':'Bearer '+key.strip(),'Content-Type':'application/json'},
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None)
    try:
        # The local desktop proxy can retain stale connections. These calls go
        # directly to the configured provider, never to result-page URLs.
        with build_opener(ProxyHandler({})).open(req,timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        messages={401:'DeepSeek 密钥未通过验证，请检查复制是否完整。',402:'DeepSeek 账户余额不足。',
            403:'当前密钥没有这个接口的访问权限。',404:'当前服务没有开放这个接口。',
            429:'DeepSeek 请求较多，请稍后重试。'}
        raise ConnectionError(messages.get(exc.code,f'DeepSeek 请求失败（HTTP {exc.code}），请重试。')) from None
    except (URLError,TimeoutError,OSError,json.JSONDecodeError):
        raise ConnectionError('暂时无法连接 DeepSeek，请检查网络后重试。') from None


def check_connection(key, preferred='deepseek-flash'):
    if not key.strip().startswith('sk-'):
        raise ConnectionError('这里需要 DeepSeek API 密钥。')
    models=[item['id'] for item in api_json('models',key).get('data',[])]
    model=preferred if preferred in models else next((m for m in ('deepseek-flash','deepseek-chat','deepseek-v4-pro') if m in models),None)
    if not model:raise ConnectionError('当前密钥没有可用的对话模型。')
    response=api_json('chat/completions',key,{'model':model,'messages':[{'role':'user','content':'请只回复：已连接'}],
        'thinking':{'type':'disabled'},'max_tokens':32,'stream':False})
    if not (response.get('choices') and response['choices'][0].get('message',{}).get('content')):raise ConnectionError('DeepSeek 没有返回连接检查结果，请重试。')
    return model

