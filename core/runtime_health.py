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
    configured = os.getenv('APP_REVISION', '').strip()
    if configured:
        return configured
    try:
        with open(os.path.join(_bootstrap.ROOT, '.release-revision'), encoding='utf-8') as handle:
            release_revision = handle.read().strip()
        if release_revision:
            return release_revision
    except OSError:
        pass
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
    return 'unknown'


_REVISION = _git_revision()


def _database_check() -> Dict[str, Any]:
    from db_compat import connect

    conn = None
    try:
        conn = connect(_bootstrap.db_path('jobs_snapshots.db'))
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        required = {
            'manual_task_runs', 'research_schema_migrations',
            'selection_runs', 'selection_artifacts', 'selection_input_manifests',
        }
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema()"
        )
        present = {str(row[0]) for row in cur.fetchall()}
        missing = sorted(required - present)
        migration = None
        if 'research_schema_migrations' in present:
            cur.execute(
                "SELECT version FROM research_schema_migrations "
                "ORDER BY applied_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            migration = str(row[0]) if row else None
        return {'ok': not missing and bool(migration), 'backend': 'postgresql',
                'schema_initialized': not missing,
                'missing_required_table_count': len(missing),
                'research_migration': migration}
    except Exception as exc:
        return {
            'ok': False,
            'backend': 'postgresql',
            'error_type': type(exc).__name__,
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _queue_check() -> Dict[str, Any]:
    """Queue schema is mandatory in a production-ready installation."""
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
            return {'ok': False, 'initialized': False, 'queued': 0, 'running': 0}
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
    expected_revision = os.getenv('EXPECTED_COMMIT', '').strip()
    revision_ok = bool(
        _REVISION != 'unknown'
        and (not expected_revision or _REVISION == expected_revision)
    )
    ready = bool(database.get('ok') and queue.get('ok') and revision_ok)
    return {
        'service': 'shadow-foliant',
        'status': 'ready' if ready else 'degraded',
        'ready': ready,
        'revision': _REVISION,
        'started_at': _STARTED_AT,
        'uptime_seconds': max(0, int(time.monotonic() - _STARTED_MONOTONIC)),
        'python': platform.python_version(),
        'checks': {
            'database': database, 'manual_queue': queue,
            'revision': {'ok': revision_ok, 'matches_expected': (
                None if not expected_revision else _REVISION == expected_revision
            )},
        },
        'features': _configuration_flags(),
    }
