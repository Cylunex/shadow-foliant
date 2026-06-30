"""Python 应用层网络探针 — 模拟 datahub 真实 HTTP 调用路径。

与 net_watch.sh(纯 curl)对照:
- 两者都 OK   → 网络此刻正常
- 都失败       → 机房出口出问题
- curl OK + 这个 FAIL → Python 层问题(httpx 连接池坏复用 / DNS 缓存 / 模块全局锁)

用途:
  # 单次跑(快速判定)
  venv2/bin/python scripts/net_probe_py.py

  # 后台长跑,每 60s 一轮,记日志
  nohup venv2/bin/python scripts/net_probe_py.py --watch 60 >> /var/log/net_probe.log 2>&1 &
  disown

  # 高并发模拟(看是不是池/锁问题)
  venv2/bin/python scripts/net_probe_py.py --concurrent 20
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx

URLS = [
    ("tencent", "https://qt.gtimg.cn/q=sh600519"),
    ("sina", "https://quotes.sina.cn/cn/api/jsonp_v2.php/var=/CN_MarketData.getKLineData?symbol=sh600519&scale=240&datalen=3"),
    ("eastmoney", "https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.600519&fields=f2"),
    ("iwencai", "https://www.iwencai.com/customized/chart/get-robot-data"),
]
HEADERS = {"User-Agent": "Mozilla/5.0"}


def probe_one(name: str, url: str, timeout: float = 8.0) -> str:
    t = time.time()
    try:
        r = httpx.get(url, timeout=timeout, headers=HEADERS)
        return f"{name:10s} code={r.status_code} {time.time()-t:.3f}s"
    except Exception as e:
        return f"{name:10s} FAIL {time.time()-t:.3f}s {type(e).__name__}: {str(e)[:80]}"


def round_once(concurrent: int = 0) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if concurrent <= 1:
        for name, url in URLS:
            print(f"{ts} {probe_one(name, url)}", flush=True)
        return
    tasks = [(name, url) for name, url in URLS for _ in range(concurrent)]
    with ThreadPoolExecutor(max_workers=concurrent * len(URLS)) as pool:
        futs = {pool.submit(probe_one, n, u): (n, u) for n, u in tasks}
        for f in as_completed(futs):
            print(f"{ts} [c={concurrent}] {f.result()}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="长跑模式,间隔秒数(0=单次)")
    ap.add_argument("--concurrent", type=int, default=0, help="每个端点并发数(0/1=串行)")
    args = ap.parse_args()
    try:
        if args.watch <= 0:
            round_once(args.concurrent)
            return 0
        while True:
            round_once(args.concurrent)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
