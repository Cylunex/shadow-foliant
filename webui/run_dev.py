"""WebUI 启动器 —— 走 .env 配置(默认连真库 PG)。

  python webui/run_dev.py                 # 用 .env(USE_POSTGRES=true → 真 PG)
  USE_POSTGRES=false python webui/run_dev.py   # 离线/无 PG 时退 SQLite
浏览器开 http://localhost:8601
"""

import os
import sys

# 不强制后端:尊重 .env 的 USE_POSTGRES(用户有内网 PG 访问,默认走真库)。
# 不依赖启动时的 cwd:切到项目根(本文件的上一级),保证 webui.api_server 可导入
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import uvicorn

if __name__ == '__main__':
    # 带时间戳的日志配置(uvicorn 默认日志不带时间);文件缺失则回退默认
    _logcfg = os.path.join(ROOT, 'webui', 'log_config.json')
    uvicorn.run('webui.api_server:app', host='127.0.0.1', port=8601, reload=False,
                log_config=_logcfg if os.path.isfile(_logcfg) else None)
