"""多 LLM 抽象层 + 自动降级

设计目标：DeepSeek 主路由；当 DeepSeek 不可用（额度耗尽/网络故障/429 限流）时
自动降级到备选 provider，保证 AI 分析任务不中断。

支持的 provider（全部走 OpenAI 兼容 SDK，约定优先级 1-99，越小越优先）：
  1. deepseek         - 方舟 Ark / DeepSeek-v4（DEEPSEEK_API_KEY）— 默认主路由
  2. siliconflow      - 硅基流动（SILICONFLOW_API_KEY）— 国内聚合，含 DeepSeek/Qwen 等
  3. tongyi           - 阿里通义千问（TONGYI_API_KEY 或 DASHSCOPE_API_KEY）
  4. gemini           - Google Gemini（GEMINI_API_KEY，需 OpenAI 兼容端点）
  5. claude           - Anthropic Claude（CLAUDE_API_KEY，通过 OpenRouter 等代理）
  6. openrouter       - OpenRouter 聚合（OPENROUTER_API_KEY）
  7. ollama           - 本地 Ollama（OLLAMA_BASE_URL，无需 key）
  99. fallback_local  - 兜底（如果用户配置了 LLM_FALLBACK_BASE_URL）

用法：
    from llm_router import get_router
    router = get_router()
    text, used = router.call(messages, temperature=0.7, max_tokens=2000)
    print(f'用了 {used}: {text[:200]}')

支持 thinking 模式（reasoner / R1 / QwQ 等）：
    text, used = router.call(messages, thinking=True)

可在 .env 中通过 LLM_PROVIDER_ORDER 强制指定顺序（逗号分隔，如
"siliconflow,deepseek,ollama"）。
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class LLMProvider:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    thinking_model: Optional[str] = None
    priority: int = 50
    enabled: bool = True
    extras: Dict = field(default_factory=dict)


_DM = os.getenv('DEEPSEEK_MODEL', os.getenv('DEFAULT_MODEL_NAME', ''))
_TM = os.getenv('DEEPSEEK_THINKING_MODEL', None)
_REGISTRY: List[LLMProvider] = [
    LLMProvider('deepseek',     os.getenv('DEEPSEEK_BASE_URL', ''), 'DEEPSEEK_API_KEY',
                _DM,                 thinking_model=_TM, priority=10),
    LLMProvider('siliconflow',  'https://api.siliconflow.cn/v1',                'SILICONFLOW_API_KEY',
                'deepseek-ai/DeepSeek-V3', thinking_model='deepseek-ai/DeepSeek-R1', priority=20),
    LLMProvider('tongyi',       'https://dashscope.aliyuncs.com/compatible-mode/v1', 'TONGYI_API_KEY',
                'qwen-plus',         thinking_model='qwq-plus', priority=30),
    LLMProvider('gemini',       'https://generativelanguage.googleapis.com/v1beta/openai/', 'GEMINI_API_KEY',
                'gemini-2.5-flash',  thinking_model='gemini-2.5-pro', priority=40),
    LLMProvider('openrouter',   'https://openrouter.ai/api/v1',                 'OPENROUTER_API_KEY',
                'deepseek/deepseek-chat', thinking_model='anthropic/claude-3.7-sonnet:thinking', priority=50),
    LLMProvider('claude',       'https://api.anthropic.com/v1',                 'CLAUDE_API_KEY',
                'claude-3-5-sonnet-latest', thinking_model='claude-3-7-sonnet-latest', priority=60),
    LLMProvider('ollama',       'http://localhost:11434/v1',                    'OLLAMA_API_KEY',
                'qwen2.5:14b',       thinking_model='deepseek-r1:14b', priority=90),
]


def _alias_envs():
    """通义/DashScope 别名 + 兼容历史 env 名"""
    if not os.getenv('TONGYI_API_KEY') and os.getenv('DASHSCOPE_API_KEY'):
        os.environ['TONGYI_API_KEY'] = os.environ['DASHSCOPE_API_KEY']


def _ollama_available() -> bool:
    """Ollama 无需 API key，但要测下本地服务在不在"""
    try:
        import requests
        base = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
        r = requests.get(f'{base}/api/version', timeout=1)
        return r.status_code == 200
    except Exception:
        return False


class LLMRouter:
    """LLM 路由器：按优先级链尝试 provider，遇错自动降级"""

    def __init__(self):
        _alias_envs()
        self.providers = self._select_active()

    def _select_active(self) -> List[LLMProvider]:
        order_str = os.getenv('LLM_PROVIDER_ORDER', '').strip()
        if order_str:
            order = [n.strip() for n in order_str.split(',') if n.strip()]
            by_name = {p.name: p for p in _REGISTRY}
            ordered = [by_name[n] for n in order if n in by_name]
            others = [p for p in _REGISTRY if p.name not in set(order)]
            chain = ordered + sorted(others, key=lambda p: p.priority)
        else:
            chain = sorted(_REGISTRY, key=lambda p: p.priority)

        active = []
        for p in chain:
            if p.name == 'ollama':
                if _ollama_available():
                    active.append(p)
                continue
            key = os.getenv(p.api_key_env, '').strip()
            if key:
                active.append(p)
        return active

    def list_active(self) -> List[Dict]:
        return [{'name': p.name, 'priority': p.priority,
                 'default_model': p.default_model,
                 'thinking_model': p.thinking_model} for p in self.providers]

    def call(self, messages: List[Dict[str, str]],
             temperature: float = 0.7, max_tokens: int = 2000,
             thinking: bool = False,
             prefer: Optional[str] = None,
             timeout: Optional[float] = None) -> Tuple[str, str]:
        """调用 LLM；返回 (response_text, used_provider_name)

        Args:
            messages: OpenAI 格式
            thinking: True 时优先用 thinking_model（如 R1/QwQ/Sonnet-Thinking）
            prefer: 强制指定 provider 名（仍带降级 fallback）
            timeout: 单 provider 调用超时(秒)。None→env LLM_TIMEOUT(默认40);thinking 取 max(timeout,120)。
                     ⚠️ 关键:openai SDK 默认超时 ~10min,无超时会让"挂起的 provider"阻塞调用方主路径
                     (选股job/持仓建议/离场)。这里强制有界:超时即抛错→降级下一 provider。
        """
        if not self.providers:
            return ('[LLM-Router] 无可用 provider，请配置至少一个 API key', 'none')

        try:
            import openai
        except ImportError:
            return ('[LLM-Router] openai SDK 未安装', 'none')

        if timeout is None:
            try:
                timeout = float(os.getenv('LLM_TIMEOUT', '40'))
            except (TypeError, ValueError):
                timeout = 40.0
        if thinking:
            timeout = max(timeout, 120.0)   # 思考模型推理更久

        chain = list(self.providers)
        if prefer:
            chain.sort(key=lambda p: 0 if p.name == prefer else 1)

        errors = []
        for p in chain:
            key = os.getenv(p.api_key_env, '') or 'ollama'
            model = p.thinking_model or p.default_model if thinking else p.default_model
            mtokens = max(max_tokens, 8000) if thinking else max_tokens
            try:
                # max_retries=0:SDK 默认重试2次会把超时×3(我们自己跨 provider 降级,不需SDK重试)
                client = openai.OpenAI(api_key=key, base_url=p.base_url, timeout=timeout, max_retries=0)
                resp = client.chat.completions.create(
                    model=model, messages=messages,
                    temperature=temperature, max_tokens=mtokens,
                    timeout=timeout,
                )
                msg = resp.choices[0].message
                result = ''
                if thinking and hasattr(msg, 'reasoning_content') and msg.reasoning_content:
                    result += f'【推理过程】\n{msg.reasoning_content}\n\n'
                if msg.content:
                    result += msg.content
                if result:
                    return result, f'{p.name}:{model}'
                errors.append(f'{p.name}:empty')
            except Exception as e:
                errors.append(f'{p.name}:{type(e).__name__}:{str(e)[:80]}')
                print(f'[LLM-Router] {p.name} 失败 → 降级: {type(e).__name__}: {str(e)[:120]}')
                continue
        return (f'[LLM-Router] 全部 provider 失败: {" | ".join(errors)}', 'none')


_singleton: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    global _singleton
    if _singleton is None:
        _singleton = LLMRouter()
    return _singleton


def reload_router() -> LLMRouter:
    """配置变更后调用，重建单例"""
    global _singleton
    _singleton = LLMRouter()
    return _singleton


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== LLM Router 自检 ===')
    r = get_router()
    active = r.list_active()
    print(f'已激活 provider: {len(active)}')
    for p in active:
        print(f"  [{p['priority']:>2}] {p['name']:>12s}  default={p['default_model']:30s}  thinking={p['thinking_model']}")

    if active:
        print('\n--- 调用测试 ---')
        msgs = [
            {'role': 'system', 'content': '你是一名 A 股资深分析师'},
            {'role': 'user', 'content': '用一句话说明北向资金对 A 股的指示意义。'}
        ]
        text, used = r.call(msgs, max_tokens=200)
        print(f'用了: {used}')
        print(f'回复: {text[:300]}')
