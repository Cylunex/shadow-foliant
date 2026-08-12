#!/bin/bash
# 一键部署 — 拉新代码 + 重启 supervisor 管的两个常驻服务,确保新 module 生效。
#
# 背景:
#   Python module 一旦 import 就被缓存, 改代码不重启进程不生效。
#   git pull 之后必须 restart, 否则跑的还是老代码(常见坑:看着部署了, 实际任务还卡死)。
#
# 用法:
#   bash scripts/deploy.sh                         # 默认 main 分支，有更新才重启
#   FORCE=1 bash scripts/deploy.sh                 # 无代码更新也重启
#   EXPECTED_COMMIT=<sha> bash scripts/deploy.sh   # 要求部署到指定提交
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"

diagnostics() {
  echo
  echo "▶ 失败诊断"
  sudo supervisorctl status stock-jobs-hub stock-webui 2>/dev/null || true
  for log_file in /var/log/supervisor/stock-jobs.log /var/log/supervisor/stock-webui.log; do
    if [[ -f "$log_file" ]]; then
      echo "--- $log_file（末 60 行）"
      sudo tail -n 60 "$log_file" 2>/dev/null || true
    fi
  done
}

CURRENT_BRANCH="$(git branch --show-current)"
if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
  echo "❌ 当前分支是 ${CURRENT_BRANCH:-detached HEAD}，要求 $DEPLOY_BRANCH"
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "❌ 工作区存在已跟踪文件改动，拒绝覆盖式部署"
  git status --short --untracked-files=no
  exit 1
fi
OLD_HEAD="$(git rev-parse HEAD)"

echo "▶ git pull --ff-only"
PULL_OUT=$(git pull --ff-only)
echo "$PULL_OUT"
NEW_HEAD="$(git rev-parse HEAD)"

if [[ -n "$EXPECTED_COMMIT" ]]; then
  if ! EXPECTED_RESOLVED="$(git rev-parse "${EXPECTED_COMMIT}^{commit}" 2>/dev/null)"; then
    echo "❌ 无法解析 EXPECTED_COMMIT=$EXPECTED_COMMIT"
    exit 1
  fi
  if [[ "$NEW_HEAD" != "$EXPECTED_RESOLVED" ]]; then
    echo "❌ 拉取后提交为 $NEW_HEAD，不是期望的 $EXPECTED_RESOLVED"
    exit 1
  fi
fi

# 脚本自身随 pull 更新时，当前 Bash 进程可能仍执行已读入的旧内容；自动用新文件重进一次。
if [[ "$OLD_HEAD" != "$NEW_HEAD" && "${DEPLOY_REEXEC:-0}" != "1" ]] \
   && git diff --name-only "$OLD_HEAD" "$NEW_HEAD" -- scripts/deploy.sh | grep -q .; then
  echo "↻ deploy.sh 已更新，切换到新版脚本继续"
  exec env DEPLOY_REEXEC=1 FORCE=1 bash scripts/deploy.sh
fi

echo
echo "▶ 配置 Supervisor 日志轮转并归档旧日志"
sudo bash scripts/configure_supervisor_logs.sh

if [[ "$OLD_HEAD" == "$NEW_HEAD" ]] && [[ "${FORCE:-0}" != "1" ]]; then
  echo "ℹ️ 代码无更新, 跳过 restart (FORCE=1 可强制重启)"
  exit 0
fi

echo
echo "▶ supervisorctl restart stock-jobs-hub stock-webui"
if ! sudo supervisorctl restart stock-jobs-hub stock-webui; then
  echo "❌ Supervisor 重启失败"
  diagnostics
  exit 1
fi

echo
echo "▶ supervisorctl status"
sudo supervisorctl status stock-jobs-hub stock-webui

echo
echo "▶ 健康检查"
WEB_OK=0
HEALTH_JSON=""
for _ in $(seq 1 20); do
  if HEALTH_JSON="$(curl -fsS --max-time 3 http://127.0.0.1:8601/api/health 2>/dev/null)"; then
    HEALTH_READY="$(printf '%s' "$HEALTH_JSON" | python3 -c \
      'import json,sys; print(str(bool((json.load(sys.stdin).get("data") or {}).get("ready"))).lower())' \
      2>/dev/null || true)"
    if [[ "$HEALTH_READY" == "true" ]]; then
      WEB_OK=1
      break
    fi
  fi
  sleep 2
done
if [[ "$WEB_OK" != "1" ]]; then
  echo "❌ Web 就绪检查失败"
  diagnostics
  exit 1
fi

HEALTH_REVISION="$(printf '%s' "$HEALTH_JSON" | python3 -c \
  'import json,sys; print((json.load(sys.stdin).get("data") or {}).get("revision", ""))' \
  2>/dev/null || true)"
if [[ "$HEALTH_REVISION" != "$NEW_HEAD" ]]; then
  echo "❌ Web 进程版本 $HEALTH_REVISION 与工作区 $NEW_HEAD 不一致"
  diagnostics
  exit 1
fi
if ! sudo supervisorctl status stock-jobs-hub stock-webui | grep -E \
    '^stock-(jobs-hub|webui)[[:space:]]+RUNNING' | grep -q 'stock-webui'; then
  echo "❌ 常驻进程未全部进入 RUNNING"
  diagnostics
  exit 1
fi
if [[ "$(sudo supervisorctl status stock-jobs-hub stock-webui | grep -Ec \
    '^stock-(jobs-hub|webui)[[:space:]]+RUNNING')" -ne 2 ]]; then
  echo "❌ 常驻进程状态数量异常"
  diagnostics
  exit 1
fi
echo "✅ Web API 与任务进程均正常，版本 ${NEW_HEAD:0:12}"

echo
echo "✅ 部署完成。看启动日志:"
echo "   sudo tail -f /var/log/supervisor/stock-jobs.log"
