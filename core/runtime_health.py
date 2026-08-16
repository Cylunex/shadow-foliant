"""轻量运行健康快照。

只检查本地依赖，不访问行情、LLM 或通知服务，避免健康检查反过来制造外部流量。
输出只包含布尔状态、计数和版本信息，不返回配置值或机器路径。
"""

from __future__ import annotations

import os
import platform
import time
from datetime import datetime
from typing import Any, Dict

import _bootstrap


_STARTED_AT = datetime.now().astimezone().isoformat(timespec='seconds')
_STARTED_MONOTONIC = time.monotonic()


def _git_revision() -> str:
    """不启动子进程读取当前提交；在模块加载时固化可识别旧进程。"""
    git_dir = os.path.join(_bootstrap.ROOT, '.git')
    try:
        with open(os.path.join(git_dir, 'HEAD'), encoding='utf-8') as handle:
            head = handle.read().strip()
        if not head.startswith('ref: '):
            return head
        ref = head[5:].strip()
        ref_path = os.path.join(git_dir, *ref.split('/'))
        if os.path.isfile(ref_path):
            with open(ref_path, encoding='utf-8') as handle:
                return handle.read().strip()
        packed = os.path.join(git_dir, 'packed-refs')
        if os.path.isfile(packed):
            with open(packed, encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    if line.startswith(('#', '^')):
                        continue
                    revision, _, name = line.strip().partition(' ')
                    if name == ref:
                        return revision
    except (OSError, ValueError):
        pass
    return os.getenv('APP_REVISION', '').strip() or 'unknown'


_REVISION = _git_revision()


def _database_check() -> Dict[str, Any]:
    from db_compat import USE_POSTGRES, connect

    conn = None
    try:
        conn = connect(_bootstrap.db_path('jobs_snapshots.db'))
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        return {'ok': True, 'backend': 'postgresql' if USE_POSTGRES else 'sqlite'}
    except Exception as exc:
        return {
            'ok': False,
            'backend': 'postgresql' if USE_POSTGRES else 'sqlite',
            'error_type': type(exc).__name__,
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _queue_check() -> Dict[str, Any]:
    """队列表不存在代表尚未使用，不把全新安装误判为故障。"""
    from db_compat import connect

    conn = None
    try:
        conn = connect(_bootstrap.db_path('jobs_snapshots.db'))
        cur = conn.cursor()
        cur.execute("""
            SELECT status, COUNT(*) FROM manual_task_runs
            WHERE status IN ('queued', 'running') GROUP BY status
        """)
        counts = {str(row[0]): int(row[1]) for row in cur.fetchall()}
        return {
            'ok': True,
            'queued': counts.get('queued', 0),
            'running': counts.get('running', 0),
        }
    except Exception as exc:
        message = str(exc).lower()
        if 'no such table' in message or 'does not exist' in message:
            return {'ok': True, 'initialized': False, 'queued': 0, 'running': 0}
        return {'ok': False, 'error_type': type(exc).__name__}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _configuration_flags() -> Dict[str, bool]:
    provider_keys = (
        'DEEPSEEK_API_KEY', 'SILICONFLOW_API_KEY', 'TONGYI_API_KEY',
        'DASHSCOPE_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY',
        'CLAUDE_API_KEY', 'ANTHROPIC_API_KEY',
    )
    return {
        'llm_configured': any(bool(os.getenv(key, '').strip()) for key in provider_keys)
                          or bool(os.getenv('OLLAMA_BASE_URL', '').strip())
                          or bool(os.getenv('SHADOW_LLM_REGISTRY_FILE', '').strip()),
        'notification_configured': any(bool(os.getenv(key, '').strip()) for key in (
            'QQ_WEBHOOK_URL', 'WEBHOOK_URL', 'EMAIL_TO')),
        'rag_enabled': os.getenv('RAG_ENABLED', 'false').strip().lower()
                       in ('1', 'true', 'yes', 'on'),
    }


def snapshot() -> Dict[str, Any]:
    database = _database_check()
    queue = _queue_check() if database.get('ok') else {'ok': False, 'skipped': True}
    ready = bool(database.get('ok') and queue.get('ok'))
    return {
        'service': 'shadow-foliant',
        'status': 'ready' if ready else 'degraded',
        'ready': ready,
        'revision': _REVISION,
        'started_at': _STARTED_AT,
        'uptime_seconds': max(0, int(time.monotonic() - _STARTED_MONOTONIC)),
        'python': platform.python_version(),
        'checks': {'database': database, 'manual_queue': queue},
        'features': _configuration_flags(),
    }
