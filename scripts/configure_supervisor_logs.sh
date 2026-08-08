#!/bin/bash
# 为 Shadow 两个 Supervisor 服务启用有界轮转，并把历史轮转文件移入日期归档目录压缩保存。
set -euo pipefail

CONFIG_FILE="${1:-/etc/supervisor/conf.d/services.conf}"
LOG_DIR="${SUPERVISOR_LOG_DIR:-/var/log/supervisor}"

if [[ "$(id -u)" != "0" && "${ALLOW_NON_ROOT_TEST:-0}" != "1" ]]; then
  echo "需要 root 权限：sudo bash scripts/configure_supervisor_logs.sh"
  exit 1
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Supervisor 配置不存在: $CONFIG_FILE"
  exit 1
fi

TMP_FILE="$(mktemp)"
python3 - "$CONFIG_FILE" "$TMP_FILE" <<'PY'
import sys

src, dst = sys.argv[1], sys.argv[2]
targets = {'program:stock-jobs-hub', 'program:stock-webui'}
with open(src, encoding='utf-8') as fh:
    lines = fh.readlines()

out = []
section = None
buffer = []

def flush(name, rows):
    if name not in targets:
        return rows
    drop = ('redirect_stderr=', 'stdout_logfile_maxbytes=',
            'stdout_logfile_backups=', 'stderr_logfile=',
            'stderr_logfile_maxbytes=', 'stderr_logfile_backups=')
    cleaned = [line for line in rows if not line.strip().startswith(drop)]
    settings = [
        'redirect_stderr=true\n',
        'stdout_logfile_maxbytes=10MB\n',
        'stdout_logfile_backups=10\n',
    ]
    insert_at = next(
        (i + 1 for i, line in enumerate(cleaned)
         if line.strip().startswith('stdout_logfile=')),
        len(cleaned),
    )
    cleaned[insert_at:insert_at] = settings
    return cleaned

for line in lines:
    stripped = line.strip()
    if stripped.startswith('[') and stripped.endswith(']'):
        out.extend(flush(section, buffer))
        section = stripped[1:-1]
        buffer = [line]
    else:
        buffer.append(line)
out.extend(flush(section, buffer))

with open(dst, 'w', encoding='utf-8') as fh:
    fh.writelines(out)
PY

if ! cmp -s "$CONFIG_FILE" "$TMP_FILE"; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONFIG_FILE" "${CONFIG_FILE}.bak-${STAMP}"
  install -m 0644 "$TMP_FILE" "$CONFIG_FILE"
  echo "✅ Supervisor 日志轮转已配置（10MB × 10 份），原配置已备份"
else
  echo "ℹ️ Supervisor 日志轮转配置已是最新"
fi
rm -f "$TMP_FILE"

STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_DIR="$LOG_DIR/archive/$STAMP"
mkdir -p "$ARCHIVE_DIR"
MOVED=0
shopt -s nullglob
for file in "$LOG_DIR"/stock-jobs.log.* "$LOG_DIR"/stock-webui.log.*; do
  [[ -f "$file" ]] || continue
  mv "$file" "$ARCHIVE_DIR/"
  MOVED=$((MOVED + 1))
done
if [[ "$MOVED" -gt 0 ]]; then
  find "$ARCHIVE_DIR" -maxdepth 1 -type f ! -name '*.gz' -exec gzip -9 {} \;
  echo "✅ 已归档并压缩 $MOVED 个历史日志: $ARCHIVE_DIR"
else
  rmdir "$ARCHIVE_DIR" 2>/dev/null || true
  echo "ℹ️ 没有待归档的历史轮转日志"
fi

"${SUPERVISORCTL_BIN:-supervisorctl}" reread
"${SUPERVISORCTL_BIN:-supervisorctl}" update
