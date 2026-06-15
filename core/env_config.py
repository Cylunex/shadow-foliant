"""环境配置(.env)读写 —— 给 WebUI 设置页用的白名单 + 脱敏读 + 原地写。

定位:让用户在「设置」页里改 API key / 数据库 / 数据源 / 通知 等 .env 配置,
不必手动编辑文件。安全取舍:

  - **白名单**:只暴露 SCHEMA 里登记的键,杜绝任意键读写。
  - **密钥脱敏**:secret 字段绝不把明文回传前端,只回 {set:bool, hint:尾4位}。
    保存时空值=保持不变(避免脱敏回显被当成新值写回覆盖)。
  - **原地写**:保留 .env 原有注释/结构,命中键就地替换,未命中追加到「# == WebUI 写入 ==」段。
  - 改完需重启进程才对已 import 的模块生效(os.getenv 多在 import 期读)——前端会提示。

只读 .env 文件本身(不读 os.environ),写回也只动 .env;.env 已 gitignore。
"""

from __future__ import annotations

import os
from typing import Dict, List, Any

import _bootstrap

ENV_PATH = os.path.join(_bootstrap.ROOT, '.env')

# 字段类型:text(普通文本) / secret(密钥,脱敏) / bool(true/false) / int(整数)
# 白名单 SCHEMA:[(key, label, group, type, help)]
SCHEMA: List[Dict[str, str]] = [
    # —— LLM 主路由 ——
    {'key': 'DEEPSEEK_API_KEY', 'label': 'DeepSeek API Key', 'group': 'LLM 主路由', 'type': 'secret',
     'help': '多智能体 / AI 研判必填'},
    {'key': 'DEEPSEEK_BASE_URL', 'label': 'DeepSeek Base URL', 'group': 'LLM 主路由', 'type': 'text',
     'help': '方舟Ark地址,如 https://ark.cn-beijing.volces.com/api/coding/v3 ;支持第三方中转/反代'},
    {'key': 'DEFAULT_MODEL_NAME', 'label': '默认模型', 'group': 'LLM 主路由', 'type': 'text',
     'help': '如 deepseek-v4-pro / qwen-plus 等'},
    {'key': 'DEEPSEEK_THINKING_MODEL', 'label': '深度思考模型', 'group': 'LLM 主路由', 'type': 'text',
     'help': '可选,留空不启用 thinking;如 deepseek-reasoner / qwq-plus'},
    {'key': 'LLM_PROVIDER_ORDER', 'label': '降级顺序', 'group': 'LLM 主路由', 'type': 'text',
     'help': '逗号分隔,如 deepseek,siliconflow,tongyi;留空按代码默认优先级'},
    {'key': 'LLM_TIMEOUT', 'label': '单 provider 超时(秒)', 'group': 'LLM 主路由', 'type': 'int',
     'help': '默认 40;thinking 模型自动放宽到 120'},
    {'key': 'EM_API_KEY', 'label': '妙想东财 Key', 'group': 'LLM 主路由', 'type': 'secret',
     'help': '可选,mx_* 工具第二意见;留空用 demo key(易限流)'},
    # —— LLM 降级(可选) —— 配齐 API_KEY + BASE_URL 才会真正进入降级链
    # 留空 → 该 provider 不激活;BASE_URL 是兜底官方端点, 用第三方反代直接改这里
    {'key': 'SILICONFLOW_API_KEY', 'label': '硅基流动 Key', 'group': 'LLM 降级(可选)', 'type': 'secret',
     'help': '留空=不启用此 provider'},
    {'key': 'SILICONFLOW_BASE_URL', 'label': '硅基流动 Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 https://api.siliconflow.cn/v1 ;用第三方中转改这里'},
    {'key': 'SILICONFLOW_MODEL', 'label': '硅基流动 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 deepseek-ai/DeepSeek-V3'},
    {'key': 'TONGYI_API_KEY', 'label': '通义千问 Key', 'group': 'LLM 降级(可选)', 'type': 'secret',
     'help': '兼容 DASHSCOPE_API_KEY;留空=不启用'},
    {'key': 'TONGYI_BASE_URL', 'label': '通义千问 Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 https://dashscope.aliyuncs.com/compatible-mode/v1'},
    {'key': 'TONGYI_MODEL', 'label': '通义千问 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 qwen-plus'},
    {'key': 'GEMINI_API_KEY', 'label': 'Gemini Key', 'group': 'LLM 降级(可选)', 'type': 'secret',
     'help': '留空=不启用'},
    {'key': 'GEMINI_BASE_URL', 'label': 'Gemini Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 https://generativelanguage.googleapis.com/v1beta/openai/'},
    {'key': 'GEMINI_MODEL', 'label': 'Gemini 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 gemini-2.5-flash'},
    {'key': 'OPENROUTER_API_KEY', 'label': 'OpenRouter Key', 'group': 'LLM 降级(可选)', 'type': 'secret',
     'help': '留空=不启用'},
    {'key': 'OPENROUTER_BASE_URL', 'label': 'OpenRouter Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 https://openrouter.ai/api/v1'},
    {'key': 'OPENROUTER_MODEL', 'label': 'OpenRouter 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 deepseek/deepseek-chat'},
    {'key': 'CLAUDE_API_KEY', 'label': 'Claude Key', 'group': 'LLM 降级(可选)', 'type': 'secret',
     'help': '兼容 ANTHROPIC_API_KEY;留空=不启用'},
    {'key': 'CLAUDE_BASE_URL', 'label': 'Claude Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 https://api.anthropic.com/v1 ;走 OpenRouter 反代请改这里'},
    {'key': 'CLAUDE_MODEL', 'label': 'Claude 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 claude-3-5-sonnet-latest'},
    {'key': 'OLLAMA_BASE_URL', 'label': '本地 Ollama Base URL', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '留空=不启用;形如 http://localhost:11434 (会自动拼 /v1 + 探活)'},
    {'key': 'OLLAMA_MODEL', 'label': '本地 Ollama 默认模型', 'group': 'LLM 降级(可选)', 'type': 'text',
     'help': '默认 qwen2.5:14b ;需先 ollama pull'},
    # —— 数据库 ——
    {'key': 'USE_POSTGRES', 'label': '启用 PostgreSQL', 'group': '数据库', 'type': 'bool',
     'help': '关 → 退 SQLite(离线/试用);盈利闭环表需 PG'},
    {'key': 'PG_HOST', 'label': 'PG 主机', 'group': '数据库', 'type': 'text', 'help': ''},
    {'key': 'PG_PORT', 'label': 'PG 端口', 'group': '数据库', 'type': 'int', 'help': ''},
    {'key': 'PG_DATABASE', 'label': 'PG 库名', 'group': '数据库', 'type': 'text', 'help': ''},
    {'key': 'PG_USER', 'label': 'PG 用户', 'group': '数据库', 'type': 'text', 'help': ''},
    {'key': 'PG_PASSWORD', 'label': 'PG 密码', 'group': '数据库', 'type': 'secret', 'help': ''},
    # —— 缓存 / 向量检索 ——
    {'key': 'REDIS_HOST', 'label': 'Redis 主机', 'group': '缓存/向量', 'type': 'text',
     'help': '缓存/分布式锁;不可达自动降级'},
    {'key': 'REDIS_PORT', 'label': 'Redis 端口', 'group': '缓存/向量', 'type': 'int', 'help': ''},
    {'key': 'REDIS_DB', 'label': 'Redis DB', 'group': '缓存/向量', 'type': 'int', 'help': ''},
    {'key': 'BGE_URL', 'label': 'BGE-M3 嵌入地址', 'group': '缓存/向量', 'type': 'text',
     'help': 'RAG 嵌入;不可达 RAG 降级,主功能不受影响'},
    {'key': 'TEI_RERANK_URL', 'label': 'TEI Rerank 地址', 'group': '缓存/向量', 'type': 'text', 'help': ''},
    # —— 数据源 ——
    {'key': 'TUSHARE_TOKEN', 'label': 'Tushare Token', 'group': '数据源', 'type': 'secret', 'help': '可选'},
    {'key': 'TDX_USE_MOOTDX', 'label': '启用 mootdx(通达信公网)', 'group': '数据源', 'type': 'bool',
     'help': '公网行情兜底,无需内网'},
    {'key': 'TDX_SERVERS', 'label': 'TDX 服务器', 'group': '数据源', 'type': 'text',
     'help': '逗号分隔 ip:port,留空自动探测'},
    {'key': 'TDX_HTTP_URL', 'label': 'TDX 内网兜底 URL', 'group': '数据源', 'type': 'text', 'help': '可选内网 HTTP'},
    # —— 通知 ——
    {'key': 'WEBHOOK_ENABLED', 'label': '启用 Webhook', 'group': '通知', 'type': 'bool', 'help': '钉钉/飞书'},
    {'key': 'WEBHOOK_TYPE', 'label': 'Webhook 类型', 'group': '通知', 'type': 'text', 'help': 'dingtalk / feishu'},
    {'key': 'WEBHOOK_URL', 'label': 'Webhook 地址', 'group': '通知', 'type': 'secret', 'help': '含 access_token,脱敏'},
    {'key': 'WEBHOOK_KEYWORD', 'label': 'Webhook 关键词', 'group': '通知', 'type': 'text', 'help': '钉钉自定义关键词'},
    {'key': 'EMAIL_ENABLED', 'label': '启用邮件', 'group': '通知', 'type': 'bool', 'help': ''},
    {'key': 'SMTP_SERVER', 'label': 'SMTP 服务器', 'group': '通知', 'type': 'text', 'help': ''},
    {'key': 'SMTP_PORT', 'label': 'SMTP 端口', 'group': '通知', 'type': 'int', 'help': ''},
    {'key': 'EMAIL_FROM', 'label': '发件邮箱', 'group': '通知', 'type': 'text', 'help': ''},
    {'key': 'EMAIL_PASSWORD', 'label': '邮箱授权码', 'group': '通知', 'type': 'secret', 'help': '非登录密码'},
    {'key': 'EMAIL_TO', 'label': '收件邮箱', 'group': '通知', 'type': 'text', 'help': ''},
    # —— 自动启动(autostart)——
    {'key': 'AUTOSTART_ENABLED', 'label': '总开关', 'group': '自动启动', 'type': 'bool',
     'help': '启动时拉起后台服务/调度'},
    {'key': 'AUTOSTART_MONITOR', 'label': '价格监测', 'group': '自动启动', 'type': 'bool', 'help': ''},
    {'key': 'AUTOSTART_JOBS_HUB', 'label': 'Jobs Hub 调度', 'group': '自动启动', 'type': 'bool',
     'help': '定时任务调度器(需常驻进程)'},
    {'key': 'AUTOSTART_NEWS_FLOW', 'label': '新闻流量调度', 'group': '自动启动', 'type': 'bool', 'help': '耗 AI token'},
]

_BY_KEY = {f['key']: f for f in SCHEMA}
_SECRET_KEYS = {f['key'] for f in SCHEMA if f['type'] == 'secret'}


def _raw_values() -> Dict[str, str]:
    """从 .env 文件解析当前值(不读 os.environ,避免拿到运行期被改过的值)。"""
    try:
        from dotenv import dotenv_values
        return {k: (v if v is not None else '') for k, v in dotenv_values(ENV_PATH).items()}
    except Exception:
        # 退化:手工解析
        out: Dict[str, str] = {}
        if not os.path.exists(ENV_PATH):
            return out
        for line in open(ENV_PATH, encoding='utf-8', errors='replace'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out


def get_config() -> List[Dict[str, Any]]:
    """返回前端用的配置项列表(密钥脱敏)。每项含 key/label/group/type/help/value/set/hint。"""
    raw = _raw_values()
    out: List[Dict[str, Any]] = []
    for f in SCHEMA:
        v = raw.get(f['key'], '')
        item: Dict[str, Any] = {**f, 'set': bool(v)}
        if f['type'] == 'secret':
            # 密钥:不回明文,仅给"是否已设置 + 尾 4 位提示"
            item['value'] = ''
            item['hint'] = ('••••' + v[-4:]) if v and len(v) >= 4 else ('已设置' if v else '')
        else:
            item['value'] = v
        out.append(item)
    return out


def _format_value(key: str, value: str) -> str:
    """写回 .env 的值格式:含空格/# 等特殊字符则加双引号;布尔归一为 true/false 小写。"""
    value = str(value).strip()
    if _BY_KEY.get(key, {}).get('type') == 'bool':
        value = 'true' if value.lower() in ('true', '1', 'yes', 'on', '是') else 'false'
    if value and (' ' in value or '#' in value or value != value.strip('"')):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def update_config(updates: Dict[str, str]) -> Dict[str, Any]:
    """原地更新 .env。仅白名单键生效;secret 字段空值=保持不变(脱敏回显不覆盖)。

    返回 {ok, changed:[...], skipped:[...]}。保留原文件注释与结构。
    """
    raw = _raw_values()
    to_write: Dict[str, str] = {}
    skipped: List[str] = []
    for k, v in (updates or {}).items():
        if k not in _BY_KEY:
            skipped.append(k)
            continue
        v = '' if v is None else str(v)
        # secret 空值 → 不动(前端不回显明文,空代表"未改")
        if k in _SECRET_KEYS and v.strip() == '':
            skipped.append(k)
            continue
        if raw.get(k, None) == v:
            skipped.append(k)
            continue
        to_write[k] = v
    if not to_write:
        return {'ok': True, 'changed': [], 'skipped': skipped}

    # 读原文件行,命中键就地替换
    lines: List[str] = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        s = line.lstrip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        key = s.split('=', 1)[0].strip()
        if key in to_write:
            lines[i] = f'{key}={_format_value(key, to_write[key])}'
            seen.add(key)
    # 未命中的键 → 追加段
    appended = [k for k in to_write if k not in seen]
    if appended:
        if lines and lines[-1].strip() != '':
            lines.append('')
        lines.append('# ===== WebUI 设置页写入 =====')
        for k in appended:
            lines.append(f'{k}={_format_value(k, to_write[k])}')

    with open(ENV_PATH, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')

    # 同步进当前进程 env(对之后才读 os.getenv 的代码即时生效;已 import 缓存的需重启)
    for k, v in to_write.items():
        os.environ[k] = v
    return {'ok': True, 'changed': list(to_write.keys()), 'skipped': skipped}


if __name__ == '__main__':
    import sys, io, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(json.dumps(get_config(), ensure_ascii=False, indent=2))
