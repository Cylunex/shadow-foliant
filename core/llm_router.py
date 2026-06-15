"""多 LLM 抽象层 + 自动降级

设计目标:DeepSeek 主路由; 当 DeepSeek 不可用(额度耗尽/网络故障/429 限流)时
自动降级到 .env 里 *已配置* 的备选 provider, 保证 AI 分析任务不中断。

⭐ 「配置了才会降级」: 一个 provider 必须满足 (a) API_KEY 非空 AND (b) BASE_URL 非空
   才会进入降级链。空 key 或空 url 直接不挂在链上。
⭐ 「base_url 不在代码里硬编码」: 每家 provider 都允许通过 .env 覆盖 base_url, 第三方
   中转/自建反代直接改 env 即可, 无需改代码。

支持的 provider(全部走 OpenAI 兼容 SDK, 优先级 1-99, 越小越优先):
  1. deepseek         主路由(DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL)
  2. siliconflow      硅基流动(SILICONFLOW_API_KEY + SILICONFLOW_BASE_URL)
  3. tongyi           阿里通义(TONGYI_API_KEY|DASHSCOPE_API_KEY + TONGYI_BASE_URL|DASHSCOPE_BASE_URL)
  4. gemini           Google Gemini(GEMINI_API_KEY + GEMINI_BASE_URL)
  5. openrouter       OpenRouter 聚合(OPENROUTER_API_KEY + OPENROUTER_BASE_URL)
  6. claude           Anthropic Claude(CLAUDE_API_KEY + CLAUDE_BASE_URL)
  9. ollama           本地 Ollama(OLLAMA_BASE_URL, 通过 /api/version 探活, 无需 key)

任何 provider 的默认 model 也可用 <NAME>_MODEL / <NAME>_THINKING_MODEL 覆盖
(留空走代码默认值)。base_url 留空 → 该 provider 不进降级链。

用法:
    from llm_router import get_router
    router = get_router()
    text, used = router.call(messages, temperature=0.7, max_tokens=2000)
    print(f'用了 {used}: {text[:200]}')

支持 thinking 模式(reasoner / R1 / QwQ 等):
    text, used = router.call(messages, thinking=True)

可在 .env 中通过 LLM_PROVIDER_ORDER 强制指定顺序(逗号分隔,如
"siliconflow,deepseek,ollama")。
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


def _env(*keys: str, default: str = '') -> str:
    """读取多个 env 别名, 第一个非空即返回。便利:既支持 TONGYI_API_KEY 也支持 DASHSCOPE_API_KEY。"""
    for k in keys:
        v = os.getenv(k, '').strip()
        if v:
            return v
    return default


def _build_registry() -> List[LLMProvider]:
    """每次构造 LLMRouter 时重建, 让 .env 变更通过 reload_router() 生效。

    base_url 优先用 env, 留空 → 代码兜底默认(官方端点);
    若 *既无 env 也无兜底* (即 base_url 为空), 该 provider 不会被激活。
    """
    return [
        LLMProvider(
            name='deepseek',
            base_url=_env('DEEPSEEK_BASE_URL'),  # 兜底空: DeepSeek 自家路径多变(方舟/官方/中转), 强制要求显式配置
            api_key_env='DEEPSEEK_API_KEY',
            default_model=_env('DEEPSEEK_MODEL', 'DEFAULT_MODEL_NAME'),
            thinking_model=_env('DEEPSEEK_THINKING_MODEL') or None,
            priority=10,
        ),
        LLMProvider(
            name='siliconflow',
            base_url=_env('SILICONFLOW_BASE_URL', default='https://api.siliconflow.cn/v1'),
            api_key_env='SILICONFLOW_API_KEY',
            default_model=_env('SILICONFLOW_MODEL', default='deepseek-ai/DeepSeek-V3'),
            thinking_model=_env('SILICONFLOW_THINKING_MODEL', default='deepseek-ai/DeepSeek-R1'),
            priority=20,
        ),
        LLMProvider(
            name='tongyi',
            base_url=_env('TONGYI_BASE_URL', 'DASHSCOPE_BASE_URL',
                          default='https://dashscope.aliyuncs.com/compatible-mode/v1'),
            api_key_env='TONGYI_API_KEY',  # DASHSCOPE_API_KEY 由 _alias_envs 复制到 TONGYI_API_KEY
            default_model=_env('TONGYI_MODEL', default='qwen-plus'),
            thinking_model=_env('TONGYI_THINKING_MODEL', default='qwq-plus'),
            priority=30,
        ),
        LLMProvider(
            name='gemini',
            base_url=_env('GEMINI_BASE_URL',
                          default='https://generativelanguage.googleapis.com/v1beta/openai/'),
            api_key_env='GEMINI_API_KEY',
            default_model=_env('GEMINI_MODEL', default='gemini-2.5-flash'),
            thinking_model=_env('GEMINI_THINKING_MODEL', default='gemini-2.5-pro'),
            priority=40,
        ),
        LLMProvider(
            name='openrouter',
            base_url=_env('OPENROUTER_BASE_URL', default='https://openrouter.ai/api/v1'),
            api_key_env='OPENROUTER_API_KEY',
            default_model=_env('OPENROUTER_MODEL', default='deepseek/deepseek-chat'),
            thinking_model=_env('OPENROUTER_THINKING_MODEL',
                                default='anthropic/claude-3.7-sonnet:thinking'),
            priority=50,
        ),
        LLMProvider(
            name='claude',
            base_url=_env('CLAUDE_BASE_URL', 'ANTHROPIC_BASE_URL',
                          default='https://api.anthropic.com/v1'),
            api_key_env='CLAUDE_API_KEY',
            default_model=_env('CLAUDE_MODEL', default='claude-3-5-sonnet-latest'),
            thinking_model=_env('CLAUDE_THINKING_MODEL', default='claude-3-7-sonnet-latest'),
            priority=60,
        ),
        LLMProvider(
            name='ollama',
            # OLLAMA_BASE_URL 习惯填 root(不带 /v1), 这里统一拼 /v1; 若用户填了 /v1 也兼容
            base_url=_normalize_ollama_url(_env('OLLAMA_BASE_URL', default='')),
            api_key_env='OLLAMA_API_KEY',
            default_model=_env('OLLAMA_MODEL', default='qwen2.5:14b'),
            thinking_model=_env('OLLAMA_THINKING_MODEL', default='deepseek-r1:14b'),
            priority=90,
        ),
    ]


def _normalize_ollama_url(url: str) -> str:
    """OLLAMA_BASE_URL 留空 → 返回空(不激活); 否则确保以 /v1 结尾。"""
    url = url.strip().rstrip('/')
    if not url:
        return ''
    return url if url.endswith('/v1') else url + '/v1'


def _alias_envs():
    """通义/DashScope 别名 + 兼容历史 env 名"""
    if not os.getenv('TONGYI_API_KEY') and os.getenv('DASHSCOPE_API_KEY'):
        os.environ['TONGYI_API_KEY'] = os.environ['DASHSCOPE_API_KEY']


def _ollama_available(base_url: str) -> bool:
    """Ollama 无需 API key, 但要测下本地服务在不在。base_url 形如 http://host:port/v1。"""
    if not base_url:
        return False
    try:
        import requests
        # /v1 → 探活点是 /api/version
        root = base_url[:-3] if base_url.endswith('/v1') else base_url
        r = requests.get(f'{root}/api/version', timeout=1)
        return r.status_code == 200
    except Exception:
        return False


class LLMRouter:
    """LLM 路由器: 按优先级链尝试 provider, 遇错自动降级到 *已配置* 的下一个 provider。"""

    def __init__(self):
        _alias_envs()
        self.providers = self._select_active()

    def _select_active(self) -> List[LLMProvider]:
        registry = _build_registry()
        order_str = os.getenv('LLM_PROVIDER_ORDER', '').strip()
        if order_str:
            order = [n.strip() for n in order_str.split(',') if n.strip()]
            by_name = {p.name: p for p in registry}
            ordered = [by_name[n] for n in order if n in by_name]
            others = [p for p in registry if p.name not in set(order)]
            chain = ordered + sorted(others, key=lambda p: p.priority)
        else:
            chain = sorted(registry, key=lambda p: p.priority)

        active: List[LLMProvider] = []
        for p in chain:
            # 双门槛: 必须同时配齐 base_url + (api_key 或 ollama 探活), 才进降级链。
            if not p.base_url:
                continue
            if p.name == 'ollama':
                if _ollama_available(p.base_url):
                    active.append(p)
                continue
            if os.getenv(p.api_key_env, '').strip():
                active.append(p)
        return active

    def list_active(self) -> List[Dict]:
        return [{'name': p.name, 'priority': p.priority,
                 'base_url': p.base_url,
                 'default_model': p.default_model,
                 'thinking_model': p.thinking_model} for p in self.providers]

    def call(self, messages: List[Dict[str, str]],
             temperature: float = 0.7, max_tokens: int = 2000,
             thinking: bool = False,
             prefer: Optional[str] = None,
             timeout: Optional[float] = None) -> Tuple[str, str]:
        """调用 LLM; 返回 (response_text, used_provider_name)

        Args:
            messages: OpenAI 格式
            thinking: True 时优先用 thinking_model(如 R1/QwQ/Sonnet-Thinking)
            prefer: 强制指定 provider 名(仍带降级 fallback)
            timeout: 单 provider 调用超时(秒)。None→env LLM_TIMEOUT(默认40);thinking 取 max(timeout,120)。
                     ⚠️ 关键:openai SDK 默认超时 ~10min, 无超时会让"挂起的 provider"阻塞调用方主路径
                     (选股 job/持仓建议/离场)。这里强制有界:超时即抛错 → 降级下一 provider。
        """
        if not self.providers:
            return ('[LLM-Router] 无可用 provider, 请至少配置一个 (API_KEY + BASE_URL)', 'none')

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

        # 只剩 1 个 provider 时, 降级日志改口径(没下家可降, 别误导)
        single = len(chain) == 1

        errors = []
        for idx, p in enumerate(chain):
            key = os.getenv(p.api_key_env, '') or 'ollama'
            model = p.thinking_model or p.default_model if thinking else p.default_model
            mtokens = max(max_tokens, 8000) if thinking else max_tokens
            try:
                # max_retries=0:SDK 默认重试 2 次会把超时×3 (我们自己跨 provider 降级, 不需 SDK 重试)
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
                # empty 也按错误对待, 走下一行的失败日志
                err_label = 'EmptyResponse'
                err_detail = '响应为空'
            except Exception as e:
                errors.append(f'{p.name}:{type(e).__name__}:{str(e)[:80]}')
                err_label = type(e).__name__
                err_detail = str(e)[:120]

            # 统一打降级日志:看是否有下家。无下家 → 明确告知"未配置降级 provider"。
            next_p = chain[idx + 1].name if idx + 1 < len(chain) else None
            if next_p:
                print(f'[LLM-Router] {p.name} 失败 → 降级到 {next_p}: {err_label}: {err_detail}',
                      flush=True)
            elif single:
                print(f'[LLM-Router] {p.name} 失败 (未配置降级 provider, 无路可降): '
                      f'{err_label}: {err_detail}', flush=True)
            else:
                print(f'[LLM-Router] {p.name} 失败 (链尾, 已无降级 provider): '
                      f'{err_label}: {err_detail}', flush=True)
            continue
        return (f'[LLM-Router] 全部 provider 失败: {" | ".join(errors)}', 'none')


_singleton: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    global _singleton
    if _singleton is None:
        _singleton = LLMRouter()
    return _singleton


def reload_router() -> LLMRouter:
    """配置变更后调用, 重建单例(重新读 .env / 重新探活 ollama)"""
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
        print(f"  [{p['priority']:>2}] {p['name']:>12s}  "
              f"base={p['base_url']:50s}  default={p['default_model']:30s}  "
              f"thinking={p['thinking_model']}")

    if not active:
        print('\n⚠️ 没有 provider 被激活。检查 .env:\n'
              '   每个 provider 都需要同时配置 <NAME>_API_KEY 和 <NAME>_BASE_URL\n'
              '   (Ollama 例外: 只看 OLLAMA_BASE_URL 探活, 不需 API key)')
    else:
        print('\n--- 调用测试 ---')
        msgs = [
            {'role': 'system', 'content': '你是一名 A 股资深分析师'},
            {'role': 'user', 'content': '用一句话说明北向资金对 A 股的指示意义。'}
        ]
        text, used = r.call(msgs, max_tokens=200)
        print(f'用了: {used}')
        print(f'回复: {text[:300]}')
