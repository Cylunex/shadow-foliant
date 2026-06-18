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
set -e

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "▶ git pull"
PULL_OUT=$(git pull)
echo "$PULL_OUT"

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
echo "✅ 部署完成。看启动日志:"
echo "   sudo tail -f /var/log/supervisor/stock-jobs.log"
