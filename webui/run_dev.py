"""WebUI 启动器 —— 走 .env 配置(默认连真库 PG)。

  python webui/run_dev.py                 # 使用 .env 中的 PostgreSQL 配置
  python webui/run_dev.py
浏览器开 http://localhost:8601
"""

import os
import sys

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
