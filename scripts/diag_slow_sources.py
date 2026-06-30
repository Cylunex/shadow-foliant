"""慢源 + 线程池 + 网络 三件套实时诊断。

用途:
  ① 区分"上游服务真慢"vs"我们 datahub 池被慢源占死"
  ② 监控 jobs-hub 进程 fd/线程数(看孤儿线程/socket 泄漏)
  ③ 同源同时刻 curl 对照

  服务器上一行跑:
    venv2/bin/python scripts/diag_slow_sources.py

  Mac 本地跑(只测网络,跳过进程监控):
    venv/bin/python scripts/diag_slow_sources.py --no-proc

  持续观察(任务挂的时段开着,看变化):
    venv2/bin/python scripts/diag_slow_sources.py --watch 30

输出说明:
  [P] 进程: fd 数 / 线程数 / TCP 连接分布     ← 单调上涨 = 实锤孤儿/socket 泄漏
  [S] 慢源直调时延                              ← 看是上游真慢还是我们调用方式不对
  [F] 快源直调时延                              ← 对照参考
  [N] 网络层 curl 同步打点                      ← 排除机房网络
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def find_pids() -> dict:
    """找两个进程:jobs-hub(定时调度)+ webui(网页"立即运行"实际跑这)。
    ⭐ webui 网页触发的任务 threading.Thread 在 webui 进程跑,
       池雪崩出现在 webui 而非 jobs-hub — 必须同时监控两者。"""
    out = {}
    patterns = [("jobs-hub", "jobs_hub|jobs\\.jobs_hub"),
                ("webui", "uvicorn.*api_server|webui\\.api_server")]
    for label, pat in patterns:
        try:
            r = subprocess.run(["pgrep", "-f", pat],
                               capture_output=True, text=True, timeout=3)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
            if pids:
                out[label] = pids[0]
        except Exception:
            pass
    return out


def proc_snapshot(pid: int) -> str:
    """fd 数 / 线程数 / ESTABLISHED 数(Linux /proc)。Mac 没有 /proc/<pid>/fd, 用 lsof。"""
    fd, threads, tcp = "?", "?", "?"
    sockets = "?"   # 仅 socket fd 数(socket:[xxx] 软链),更直接反映"网络连接"
    if os.path.isdir(f"/proc/{pid}/fd"):
        try:
            fd_dir = f"/proc/{pid}/fd"
            entries = os.listdir(fd_dir)
            fd = str(len(entries))
            # 统计 socket: 那些 readlink 出来是 'socket:[xxx]' 的 fd
            sk = 0
            for e in entries:
                try:
                    if os.readlink(f"{fd_dir}/{e}").startswith("socket:"):
                        sk += 1
                except Exception:
                    pass
            sockets = str(sk)
        except Exception:
            pass
        try:
            threads = str(len(os.listdir(f"/proc/{pid}/task")))
        except Exception:
            pass
        # ⭐ 用 PID 过滤,只数本进程的 ESTAB 连接(否则是整机数,严重失真)
        try:
            r = subprocess.run(["ss", "-tnp"], capture_output=True, text=True, timeout=3)
            tcp = str(sum(1 for l in r.stdout.splitlines()
                          if f"pid={pid}," in l and "ESTAB" in l))
        except Exception:
            pass
    else:
        # Mac fallback
        try:
            r = subprocess.run(["lsof", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5)
            lines = r.stdout.splitlines()[1:]
            fd = str(len(lines))
            tcp = str(sum(1 for l in lines if "TCP" in l and "ESTAB" in l))
            sockets = str(sum(1 for l in lines if "IPv4" in l or "IPv6" in l))
        except Exception:
            pass
    return f"fd={fd} sockets={sockets} threads={threads} tcp_est={tcp}"


def time_call(label: str, fn, *args, **kwargs) -> str:
    """跑 fn,记时延 + 结果概要。"""
    t = time.time()
    try:
        v = fn(*args, **kwargs)
        el = time.time() - t
        if hasattr(v, "empty"):
            sz = f"DataFrame({len(v)} rows)" if not v.empty else "EMPTY"
        elif isinstance(v, dict):
            sz = f"dict({len(v)} keys)"
        elif isinstance(v, list):
            sz = f"list({len(v)})"
        elif v is None:
            sz = "None"
        else:
            sz = f"{type(v).__name__}"
        return f"{label:30s} {el:6.2f}s {sz}"
    except Exception as e:
        return f"{label:30s} {time.time()-t:6.2f}s FAIL {type(e).__name__}: {str(e)[:60]}"


def curl_one(url: str, name: str, timeout: int = 8) -> str:
    """curl 同步打一个端点,只取 total time。"""
    try:
        r = subprocess.run(
            ["curl", "-m", str(timeout), "-sS", "-o", "/dev/null",
             "-w", "code=%{http_code} total=%{time_total}s",
             url, "-A", "Mozilla/5.0"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return f"{name:12s} {r.stdout.strip()}"
    except Exception as e:
        return f"{name:12s} FAIL {type(e).__name__}: {str(e)[:60]}"


def run_round(do_proc: bool, do_datahub: bool) -> None:
    print(f"\n========== {now()} ==========")

    # [P] 进程状态(同时打 jobs-hub + webui;网页触发跑在 webui!)
    if do_proc:
        pids = find_pids()
        if pids:
            for label, pid in pids.items():
                print(f"[P] {label:8s} pid={pid}  {proc_snapshot(pid)}")
        else:
            print("[P] 进程没找到(本机/没起?)")

    # [N] 网络层 curl(跟 datahub 调用完全无关,排除机房网络)
    print("[N] 网络层 curl:")
    for url, name in [
        ("https://qt.gtimg.cn/q=sh600519", "tencent"),
        ("https://hq.sinajs.cn/list=sh600519", "sina"),
        ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.600519&fields=f2", "eastmoney"),
    ]:
        print(f"    {curl_one(url, name)}")

    if not do_datahub:
        return

    # 加载 datahub(进程内,不通过 supervisor)
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import _bootstrap  # noqa
        import datahub
    except Exception as e:
        print(f"[!] datahub 加载失败: {e}")
        return

    # [F] 快源 — 应当秒回
    print("[F] 快源直调(应秒回):")
    print(f"    {time_call('datahub.quotes(600519)', datahub.quotes, ['600519'])}")
    print(f"    {time_call('datahub.indices()', datahub.indices)}")
    print(f"    {time_call('datahub.kline(600519, 6mo, raw)', datahub.kline, '600519', period='6mo', adjust='raw')}")

    # [S] 慢源 — 重点
    print("[S] 慢源直调(关键!):")
    # 妙想 selectSecurity
    try:
        from analysis.miaoxiang import screen as mx_screen
        print(f"    {time_call('miaoxiang.screen(主力)', mx_screen, '今日主力净流入前10', select_type='A股')}")
    except Exception as e:
        print(f"    miaoxiang.screen           FAIL {type(e).__name__}: {str(e)[:60]}")
    # 问财主力资金 query(复杂 query 是已知慢点)
    try:
        from data.sources.pywencai import pywencai_get
        q = "2026年6月25日以来主力资金净流入排名,市值50-5000亿之间,非科创非st"
        print(f"    {time_call('pywencai(主力资金长query)', pywencai_get, q, timeout=30)}")
    except Exception as e:
        print(f"    pywencai                   FAIL {type(e).__name__}: {str(e)[:60]}")
    # full_valuation(同花顺,曾经的雪崩源头)
    try:
        print(f"    {time_call('datahub.full_valuation(600519)', datahub.full_valuation, '600519')}")
    except Exception as e:
        print(f"    full_valuation              FAIL {type(e).__name__}: {str(e)[:60]}")

    # [R] datahub 池/源健康度
    try:
        unhealthy = datahub.unhealthy_sources()
        if unhealthy:
            print(f"[R] 冷却中的源({len(unhealthy)}):")
            for u in unhealthy[:10]:
                print(f"    - {u}")
        else:
            print("[R] 所有源都健康(无冷却)")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="持续监控,间隔秒数(0=单次)")
    ap.add_argument("--no-proc", action="store_true", help="跳过进程监控(本机/没 jobs-hub)")
    ap.add_argument("--no-datahub", action="store_true", help="跳过 datahub 加载(只看网络+进程)")
    args = ap.parse_args()
    try:
        if args.watch <= 0:
            run_round(do_proc=not args.no_proc, do_datahub=not args.no_datahub)
            return 0
        while True:
            run_round(do_proc=not args.no_proc, do_datahub=not args.no_datahub)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
