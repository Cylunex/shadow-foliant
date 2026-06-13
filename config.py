import os
from dotenv import load_dotenv

# 加载环境变量（override=True 强制覆盖已存在的环境变量）
load_dotenv(override=True)

# DeepSeek API配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "")
DEEPSEEK_BASE_URL_WAS_SET = bool(DEEPSEEK_BASE_URL)

# 默认AI模型名称（支持任何OpenAI兼容的模型）
DEFAULT_MODEL_NAME = os.getenv("DEFAULT_MODEL_NAME", "")

# 运行时校验
if not DEEPSEEK_BASE_URL:
    raise RuntimeError(
        "❌ DEEPSEEK_BASE_URL 未配置！请在 .env 中设置方舟（Ark）API 地址，"
        "例如：DEEPSEEK_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3"
    )

# 其他配置
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

# 股票数据源配置
DEFAULT_PERIOD = "1y"  # 默认获取1年数据
DEFAULT_INTERVAL = "1d"  # 默认日线数据

# MiniQMT量化交易配置
MINIQMT_CONFIG = {
    'enabled': os.getenv("MINIQMT_ENABLED", "false").lower() == "true",
    'account_id': os.getenv("MINIQMT_ACCOUNT_ID", ""),
    'host': os.getenv("MINIQMT_HOST", "127.0.0.1"),
    'port': int(os.getenv("MINIQMT_PORT", "58610")),
}

# TDX股票数据API配置项目地址github.com/oficcejo/tdx-api
TDX_CONFIG = {
    'enabled': os.getenv("TDX_ENABLED", "false").lower() == "true",
    'base_url': os.getenv("TDX_BASE_URL", "http://127.0.0.1:8181"),
}

# 网络代理配置（全项目统一入口，改一处即生效）
PROXY = os.getenv("PROXY_URL") or None
PROXIES = {'http': PROXY, 'https': PROXY} if PROXY else None