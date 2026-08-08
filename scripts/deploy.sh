#!/bin/bash
# 一键部署 — 拉新代码 + 重启 supervisor 管的两个常驻服务,确保新 module 生效。
#
# 背景:
#   Python module 一旦 import 就被缓存, 改代码不重启进程不生效。
#   git pull 之后必须 restart, 否则跑的还是老代码(常见坑:看着部署了, 实际任务还卡死)。
#
# 用法:
#   bash scripts/deploy.sh           # 默认 supervisorctl restart
#   FORCE=1 bash scripts/deploy.sh   # 即使 git pull 没改动也 restart
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "▶ git pull --ff-only"
PULL_OUT=$(git pull --ff-only)
echo "$PULL_OUT"

echo
echo "▶ 配置 Supervisor 日志轮转并归档旧日志"
sudo bash scripts/configure_supervisor_logs.sh

if [[ "$PULL_OUT" == *"Already up to date"* ]] && [[ "${FORCE:-0}" != "1" ]]; then
  echo "ℹ️ 代码无更新, 跳过 restart (FORCE=1 可强制重启)"
  exit 0
fi

echo
echo "▶ supervisorctl restart stock-jobs-hub stock-webui"
sudo supervisorctl restart stock-jobs-hub stock-webui

echo
echo "▶ supervisorctl status"
sudo supervisorctl status stock-jobs-hub stock-webui

echo
echo "▶ 健康检查"
WEB_OK=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 http://127.0.0.1:8601/api/health >/dev/null; then
    WEB_OK=1
    break
  fi
  sleep 2
done
if [[ "$WEB_OK" != "1" ]]; then
  echo "❌ Web 健康检查失败，请检查 /var/log/supervisor/stock-webui.log"
  exit 1
fi
sudo supervisorctl status stock-jobs-hub | grep -q RUNNING
echo "✅ Web API 与任务进程均正常"

echo
echo "✅ 部署完成。看启动日志:"
echo "   sudo tail -f /var/log/supervisor/stock-jobs.log"
