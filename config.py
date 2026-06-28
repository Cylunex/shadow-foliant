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

# 选股是否排除科创板（688/689）。默认排除（True）—— 科创板涨跌幅±20%、风险高、散户权限门槛高。
# 全项目选股统一入口:问财 5 策略本就在 query 里写"非科创板",妙想镜像/多因子等其余源由 unified_selection
# 的候选过滤兜底。设 env EXCLUDE_KCB=false 可放开纳入科创板。
EXCLUDE_KCB = os.getenv("EXCLUDE_KCB", "true").lower() not in ("false", "0", "no", "off")

# 多因子选股的默认 universe(指数成分股池)。默认 000510=中证A500(逐行业选龙头、ESG筛、
# 横跨大中小盘,比沪深300更均衡、更偏新经济,对技术因子选股是更优默认池)。
# 可选:000300沪深300(纯大盘) / 000905中证500(纯中盘) / 000852中证1000(小盘)。
# 改 env SELECTION_INDEX_UNIVERSE 可回退或 A/B(缓存键含 index_code,切换天然换键不串味;
# 盘后焐与早盘读共用此默认,保持 cache_only 命中)。仅作用于多因子选股族,不影响 5 大问财策略/妙想。
SELECTION_INDEX_UNIVERSE = os.getenv("SELECTION_INDEX_UNIVERSE", "000510")