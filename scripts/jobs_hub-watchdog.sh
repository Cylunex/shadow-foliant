#!/bin/bash
# jobs_hub 看门狗 —— 独立守护进程(可移植版,无绝对路径)
#
# 作用:常驻后台,每 2 分钟检查一次 jobs_hub(定时任务调度器)进程是否存活,
#       挂了就自动拉起。Windows 无 bash,本脚本面向 Linux/macOS/OpenClaw 常驻环境。
#
# ── 路径 ──────────────────────────────────────────────────────────────
#   全部相对脚本自身位置推导,克隆/部署到任意目录都自洽,勿再硬编码盘符或 ~/.openclaw。
#   PROJECT_DIR = 本脚本所在 scripts/ 的上一级(项目根)
#   日志/PID     = 项目根下 logs/(已在 .gitignore)
#   可用环境变量覆盖:JOBS_HUB_PROJECT_DIR / JOBS_HUB_LOG_DIR / JOBS_HUB_PYTHON / JOBS_HUB_TZ
#
# ── 用法 ──────────────────────────────────────────────────────────────
#   bash scripts/jobs_hub-watchdog.sh start     # 后台启动看门狗(它再拉起 jobs_hub)
#   bash scripts/jobs_hub-watchdog.sh stop      # 停看门狗 + jobs_hub
#   bash scripts/jobs_hub-watchdog.sh restart    # 重启
#   bash scripts/jobs_hub-watchdog.sh status     # 查看两者状态
#
#   常驻部署(OpenClaw/服务器)建议把 `... start` 写进开机自启 / supervisor / systemd,
#   或直接用 systemd/supervisor 守护 `python3 -m jobs.jobs_hub --serve` 取代本看门狗。
# ──────────────────────────────────────────────────────────────────────

set -u

# 脚本所在目录(兼容软链接调用)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

PROJECT_DIR="${JOBS_HUB_PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_DIR="${JOBS_HUB_LOG_DIR:-$PROJECT_DIR/logs}"
# 优先用项目 venv2 下的 python3（存有全部依赖），退到系统 python3
_VENV_PY="$PROJECT_DIR/venv2/bin/python3"
if [ -x "$_VENV_PY" ]; then
    PYTHON_BIN="${JOBS_HUB_PYTHON:-$_VENV_PY}"
else
    PYTHON_BIN="${JOBS_HUB_PYTHON:-python3}"
fi
RUN_TZ="${JOBS_HUB_TZ:-Asia/Shanghai}"

LOG_FILE="$LOG_DIR/jobs_hub-watchdog.log"
PID_FILE="$LOG_DIR/.jobs_hub.pid"
WATCHDOG_PID_FILE="$LOG_DIR/.jobs_hub-watchdog.pid"

mkdir -p "$LOG_DIR"

_watchdog_loop() {
    echo "[watchdog] 🐶 jobs_hub 看门狗启动 (PID: $$) | 项目: $PROJECT_DIR" | tee -a "$LOG_FILE"
    while true; do
        if [ -f "$PID_FILE" ]; then
            HUB_PID=$(cat "$PID_FILE" 2>/dev/null)
            if [ -n "$HUB_PID" ] && kill -0 "$HUB_PID" 2>/dev/null; then
                : # 活着
            else
                echo "[watchdog] ❌ jobs_hub (PID $HUB_PID) 已死亡，重启中..." | tee -a "$LOG_FILE"
                _start_hub
            fi
        else
            echo "[watchdog] ⚠️ 无 PID 文件，启动 jobs_hub..." | tee -a "$LOG_FILE"
            _start_hub
        fi
        sleep 120
    done
}

_start_hub() {
    cd "$PROJECT_DIR" || { echo "[watchdog] ❌ 无法进入项目目录 $PROJECT_DIR" | tee -a "$LOG_FILE"; return 1; }
    # ⭐ 先清理残留进程,防止多实例并行
    pkill -f "jobs.jobs_hub" 2>/dev/null; sleep 1
    TZ="$RUN_TZ" PYTHONUNBUFFERED=1 "$PYTHON_BIN" -m jobs.jobs_hub --serve >> "$LOG_DIR/jobs_hub-daemon.log" 2>&1 &
    HUB_PID=$!
    echo "$HUB_PID" > "$PID_FILE"
    echo "[watchdog] ✅ jobs_hub 已启动 (PID: $HUB_PID)" | tee -a "$LOG_FILE"
}

_start_watchdog() {
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        WPID=$(cat "$WATCHDOG_PID_FILE")
        if kill -0 "$WPID" 2>/dev/null; then
            echo "看门狗已在运行 (PID: $WPID)"
            return 0
        fi
    fi
    # 后台启动看门狗
    nohup bash "$0" _loop > /dev/null 2>&1 &
    echo "$!" > "$WATCHDOG_PID_FILE"
    echo "✅ 看门狗已启动 (PID: $!) | 日志: $LOG_FILE"
}

_stop() {
    if [ -f "$PID_FILE" ]; then
        HUB_PID=$(cat "$PID_FILE")
        kill "$HUB_PID" 2>/dev/null && echo "✅ jobs_hub 已停止 (PID: $HUB_PID)" || echo "⚠️ jobs_hub 未运行"
        rm -f "$PID_FILE"
    fi
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        WPID=$(cat "$WATCHDOG_PID_FILE")
        kill "$WPID" 2>/dev/null && echo "✅ 看门狗已停止" || echo "⚠️ 看门狗未运行"
        rm -f "$WATCHDOG_PID_FILE"
    fi
}

_status() {
    local status_str=""
    if [ -f "$PID_FILE" ]; then
        HUB_PID=$(cat "$PID_FILE")
        if kill -0 "$HUB_PID" 2>/dev/null; then
            status_str+="✅ jobs_hub 运行中 (PID: $HUB_PID)\\n"
        else
            status_str+="❌ jobs_hub PID 文件存在但进程已死亡\\n"
        fi
    else
        status_str+="❌ jobs_hub 未启动\\n"
    fi
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        WPID=$(cat "$WATCHDOG_PID_FILE")
        if kill -0 "$WPID" 2>/dev/null; then
            status_str+="✅ 看门狗运行中 (PID: $WPID)"
        else
            status_str+="❌ 看门狗 PID 文件存在但进程已死亡"
        fi
    else
        status_str+="❌ 看门狗未启动"
    fi
    echo -e "$status_str"
}

case "${1:-}" in
    _loop)   _watchdog_loop ;;
    start)   _start_watchdog ;;
    stop)    _stop ;;
    restart) _stop; sleep 1; _start_watchdog ;;
    status)  _status ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        echo "  start   后台启动看门狗(自动拉起并守护 jobs_hub)"
        echo "  stop    停止看门狗与 jobs_hub"
        echo "  restart 重启"
        echo "  status  查看状态"
        echo "  可选 env: JOBS_HUB_PROJECT_DIR / JOBS_HUB_LOG_DIR / JOBS_HUB_PYTHON / JOBS_HUB_TZ"
        exit 1
        ;;
esac
