#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent/Web 共用的手动任务控制层。

定时调度仍由 jobs_hub 负责；本模块只处理按需触发：
  - 提交后立即返回 run_id，不阻塞 MCP/HTTP
  - PostgreSQL 持久队列由常驻 jobs_hub 消费，提交进程退出也不会丢任务
  - worker 心跳 + 超时重入队，jobs_hub 重启后可恢复未完成任务
  - 支持幂等键，避免 Agent 重试造成同一任务重复执行
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from db_compat import connect as db_connect, USE_POSTGRES


_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')
_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_STALE_CHECK_LOCK = threading.Lock()
_LAST_STALE_CHECK = 0.0

_JOB_FN_ALIAS = {'wf_weekly_backtest': 'task_weekly_backtest'}
_SUBSTEP_FNS = {
    'wf_daily_strategy_scan': '_daily_strategy_scan',
    'wf_daily_candidate_pool': '_daily_candidate_pool',
    'wf_position_profit_check': '_position_profit_check',
    'wf_position_guard_check': '_position_guard_check',
}
_JOB_FN_ALIAS.update(_SUBSTEP_FNS)
_INLINE_PARENT = {
    'wf_daily_pattern_alert': 'portfolio_indicator_snapshot',
    'wf_overnight_to_rec': 'morning_strategy',
    'wf_selection_to_rec': 'unified_selection',
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _today_bounds() -> Tuple[str, str]:
    """返回本地时区今天的半开区间，避免 SQLite DATE('now') 按 UTC 错日。"""
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _task_dependencies(task_name: str) -> Tuple[str, ...]:
    try:
        from automation_config import REGISTRY
        raw = REGISTRY.get(task_name, {}).get('depends_on') or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(str(name) for name in raw if name)
    except Exception:
        return ()


def _dependency_enabled(task_name: str) -> bool:
    try:
        from automation_config import is_enabled
        return bool(is_enabled(task_name))
    except Exception:
        # 配置层不可用时保持原有 fail-open 语义。
        return True


def _dependency_succeeded_today(task_name: str) -> bool:
    start, end = _today_bounds()
    conn = None
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT 1 FROM job_runs
            WHERE job_name = ? AND status = 'success'
              AND started_at >= ? AND started_at < ?
            LIMIT 1
        ''', (task_name, start, end))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def _dependency_active(task_name: str) -> Optional[Dict[str, Any]]:
    """已有手动/定时上游在排队或运行时复用它，避免重复执行重任务。"""
    if _scheduled_task_running(task_name):
        return {'task_name': task_name, 'status': 'running', 'kind': 'scheduled'}
    conn = None
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT run_id, status FROM manual_task_runs
            WHERE task_name = ? AND status IN ('queued', 'running')
            ORDER BY requested_at DESC LIMIT 1
        ''', (task_name,))
        row = cur.fetchone()
        if row:
            return {
                'task_name': task_name, 'run_id': row[0],
                'status': str(row[1]), 'kind': 'manual',
            }
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return None


def _auto_submit_dependencies(task_name: str, max_attempts: int,
                              seen: set) -> List[Dict[str, Any]]:
    """为 Agent/Web 手动任务补齐未完成上游，保证单 worker 也不会依赖死锁。"""
    results: List[Dict[str, Any]] = []
    for dependency in _task_dependencies(task_name):
        if dependency in seen:
            chain = ' -> '.join(list(seen) + [dependency])
            raise ValueError(f'任务依赖存在循环: {chain}')
        if not _dependency_enabled(dependency):
            results.append({'task_name': dependency, 'status': 'disabled'})
            continue
        if _dependency_succeeded_today(dependency):
            results.append({'task_name': dependency, 'status': 'satisfied'})
            continue
        active = _dependency_active(dependency)
        if active:
            results.append(active)
            continue
        run = _submit_task(
            dependency,
            requested_by='dependency',
            idempotency_key=f'auto:{datetime.now().astimezone().date().isoformat()}:{dependency}',
            max_attempts=max_attempts,
            seen=seen | {task_name},
        )
        results.append({
            'task_name': dependency,
            'status': run.get('status'),
            'run_id': run.get('run_id'),
            'duplicate': bool(run.get('duplicate')),
        })
    return results


def _init_db() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        if not USE_POSTGRES:
            # 仅供隔离单测在显式注入临时连接时先于 jobs_hub 初始化。
            cur.execute('''
                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS indicator_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    indicators TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, snapshot_date)
                )
            ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS manual_task_runs (
                run_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                worker_id TEXT,
                heartbeat_at TEXT,
                UNIQUE(requested_by, task_name, idempotency_key)
            )
        ''')
        # 兼容上一版已经创建的表。PG 可 IF NOT EXISTS；SQLite 先看 PRAGMA。
        new_columns = {
            'attempts': 'INTEGER NOT NULL DEFAULT 0',
            'max_attempts': 'INTEGER NOT NULL DEFAULT 2',
            'worker_id': 'TEXT',
            'heartbeat_at': 'TEXT',
        }
        if USE_POSTGRES:
            for name, ddl in new_columns.items():
                cur.execute(f'ALTER TABLE manual_task_runs ADD COLUMN IF NOT EXISTS {name} {ddl}')
        else:
            cur.execute('PRAGMA table_info(manual_task_runs)')
            existing = {row[1] for row in cur.fetchall()}
            for name, ddl in new_columns.items():
                if name not in existing:
                    cur.execute(f'ALTER TABLE manual_task_runs ADD COLUMN {name} {ddl}')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_manual_task_runs_recent
            ON manual_task_runs(requested_at)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_manual_task_runs_task
            ON manual_task_runs(task_name, requested_at)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_manual_task_runs_queue
            ON manual_task_runs(status, requested_at)
        ''')
        conn.commit()
        conn.close()
        _INITIALIZED = True


def _resolve_task(name: str):
    if name in _INLINE_PARENT:
        parent = _INLINE_PARENT[name]
        raise ValueError(
            f'「{name}」是并入「{parent}」的内联子步骤，不能单独触发；请触发「{parent}」'
        )
    from jobs import jobs_hub
    fn_name = _JOB_FN_ALIAS.get(name, f'task_{name}')
    fn = getattr(jobs_hub, fn_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f'未知或不可手动触发的任务: {name}')
    return fn


def _json_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps({'repr': repr(value)}, ensure_ascii=False)
    # 防止异常任务把大段报告重复塞进运行表；完整业务结果应进入 artifact/业务表。
    return text[:20000]


def _decode_result(value: Any) -> Any:
    if value in (None, ''):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _row_to_dict(row) -> Dict[str, Any]:
    keys = [
        'run_id', 'task_name', 'requested_by', 'idempotency_key', 'status',
        'requested_at', 'started_at', 'finished_at', 'result_json', 'error',
        'attempts', 'max_attempts', 'worker_id', 'heartbeat_at',
    ]
    out = dict(zip(keys, row))
    out['result'] = _decode_result(out.pop('result_json', None))
    return out


def recover_stale_runs(stale_after_seconds: int = 180) -> Dict[str, int]:
    """回收失联 worker。

    有剩余尝试次数的任务回到 queued；超过 max_attempts 才标 interrupted。
    正常长任务由独立心跳线程续期，不会因运行时间长被误回收。
    """
    _init_db()
    global _LAST_STALE_CHECK
    with _STALE_CHECK_LOCK:
        now_mono = time.monotonic()
        if _LAST_STALE_CHECK and now_mono - _LAST_STALE_CHECK < 60:
            return {'requeued': 0, 'interrupted': 0}
    cutoff = (datetime.now().astimezone() - timedelta(seconds=stale_after_seconds)).isoformat(
        timespec='seconds')
    now = _now()
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        UPDATE manual_task_runs
        SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
            started_at = NULL, error = 'worker 心跳超时，已自动重新入队'
        WHERE status = 'running'
          AND COALESCE(heartbeat_at, started_at, requested_at) < ?
          AND attempts < max_attempts
    ''', (cutoff,))
    requeued = cur.rowcount
    cur.execute('''
        UPDATE manual_task_runs
        SET status = 'interrupted', finished_at = ?, worker_id = NULL,
            error = COALESCE(error, 'worker 心跳超时且已达到最大尝试次数')
        WHERE status = 'running'
          AND COALESCE(heartbeat_at, started_at, requested_at) < ?
          AND attempts >= max_attempts
    ''', (now, cutoff))
    interrupted = cur.rowcount
    # 兼容旧 worker/提交失败留下的“已耗尽但仍 queued”记录。
    cur.execute('''
        UPDATE manual_task_runs
        SET status = 'interrupted', finished_at = ?, worker_id = NULL,
            error = COALESCE(error, '已达到最大尝试次数')
        WHERE status = 'queued' AND attempts >= max_attempts
    ''', (now,))
    interrupted += cur.rowcount
    conn.commit()
    conn.close()
    with _STALE_CHECK_LOCK:
        _LAST_STALE_CHECK = time.monotonic()
    return {'requeued': requeued, 'interrupted': interrupted}


def get_task_run(run_id: str) -> Optional[Dict[str, Any]]:
    _init_db()
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT run_id, task_name, requested_by, idempotency_key, status,
               requested_at, started_at, finished_at, result_json, error,
               attempts, max_attempts, worker_id, heartbeat_at
        FROM manual_task_runs WHERE run_id = ?
    ''', (run_id,))
    row = cur.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def list_task_runs(task_name: str = '', limit: int = 30) -> List[Dict[str, Any]]:
    _init_db()
    limit = max(1, min(int(limit), 200))
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if task_name:
        cur.execute('''
            SELECT run_id, task_name, requested_by, idempotency_key, status,
                   requested_at, started_at, finished_at, result_json, error,
                   attempts, max_attempts, worker_id, heartbeat_at
            FROM manual_task_runs
            WHERE task_name = ?
            ORDER BY requested_at DESC LIMIT ?
        ''', (task_name, limit))
    else:
        cur.execute('''
            SELECT run_id, task_name, requested_by, idempotency_key, status,
                   requested_at, started_at, finished_at, result_json, error,
                   attempts, max_attempts, worker_id, heartbeat_at
            FROM manual_task_runs
            ORDER BY requested_at DESC LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_row_to_dict(row) for row in rows]


def _existing_idempotent_run(requested_by: str, task_name: str,
                             idempotency_key: str) -> Optional[Dict[str, Any]]:
    if not idempotency_key:
        return None
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT run_id FROM manual_task_runs
        WHERE requested_by = ? AND task_name = ? AND idempotency_key = ?
        LIMIT 1
    ''', (requested_by, task_name, idempotency_key))
    row = cur.fetchone()
    conn.close()
    return get_task_run(row[0]) if row else None


def _submit_task(task_name: str, requested_by: str, idempotency_key: str,
                 max_attempts: int, seen: set) -> Dict[str, Any]:
    _resolve_task(task_name)  # 提交前先校验，避免产生永远 queued 的脏记录
    _init_db()
    requested_by = (requested_by or 'agent').strip()[:40]
    idempotency_key = (idempotency_key or '').strip()[:160]
    max_attempts = max(1, min(int(max_attempts), 5))

    existing = _existing_idempotent_run(requested_by, task_name, idempotency_key)
    if existing:
        return {'accepted': True, 'duplicate': True, **existing}

    dependencies = _auto_submit_dependencies(task_name, max_attempts, seen)

    run_id = uuid.uuid4().hex
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute('''
            INSERT INTO manual_task_runs(
                run_id, task_name, requested_by, idempotency_key, status, requested_at,
                attempts, max_attempts
            ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?)
        ''', (run_id, task_name, requested_by, idempotency_key or None, _now(), max_attempts))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        # 两个 Agent 并发提交相同幂等键时，由唯一约束裁决，返回胜出的记录。
        existing = _existing_idempotent_run(requested_by, task_name, idempotency_key)
        if existing:
            return {'accepted': True, 'duplicate': True, **existing}
        raise
    conn.close()

    return {
        'accepted': True,
        'duplicate': False,
        'run_id': run_id,
        'task_name': task_name,
        'status': 'queued',
        'poll_after_seconds': 2,
        'dependencies': dependencies,
    }


def submit_task(task_name: str, requested_by: str = 'agent',
                idempotency_key: str = '', max_attempts: int = 2) -> Dict[str, Any]:
    """提交后台任务并立即返回。

    相同来源+任务+幂等键只执行一次；有上游依赖时自动补齐，并由领取器保证上游先跑。
    """
    return _submit_task(task_name, requested_by, idempotency_key, max_attempts, seen=set())


def _update_run(run_id: str, *, status: str, started_at: str = None,
                finished_at: str = None, result: Any = None, error: str = None,
                heartbeat_at: str = None) -> None:
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        UPDATE manual_task_runs
        SET status = ?,
            started_at = COALESCE(?, started_at),
            finished_at = COALESCE(?, finished_at),
            result_json = COALESCE(?, result_json),
            error = COALESCE(?, error),
            heartbeat_at = COALESCE(?, heartbeat_at)
        WHERE run_id = ?
    ''', (status, started_at, finished_at, _json_value(result), error, heartbeat_at, run_id))
    conn.commit()
    conn.close()


def _latest_job_outcome(task_name: str, started_at: str) -> Tuple[str, Optional[str]]:
    """任务函数通常自己写 job_runs；用它识别内部捕获但未重新抛出的失败。"""
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT status, error FROM job_runs
            WHERE job_name = ? AND started_at >= ?
            ORDER BY id DESC LIMIT 1
        ''', (task_name, started_at))
        row = cur.fetchone()
        conn.close()
        if row:
            return str(row[0] or 'success'), row[1]
    except Exception:
        pass
    return 'success', None


def claim_next_task(worker_id: str) -> Optional[Dict[str, Any]]:
    """原子领取一条依赖已满足的 queued 任务。

    等上游的任务不占 worker；上游明确失败后，下游按 skipped 收尾。
    """
    _init_db()
    recover_stale_runs()
    worker_id = (worker_id or 'jobs_hub').strip()[:120]
    now = _now()
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    try:
        if USE_POSTGRES:
            cur.execute('''
                SELECT run_id, task_name FROM manual_task_runs
                WHERE status = 'queued' AND attempts < max_attempts
                ORDER BY requested_at
                FOR UPDATE SKIP LOCKED LIMIT 50
            ''')
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('''
                SELECT run_id, task_name FROM manual_task_runs
                WHERE status = 'queued' AND attempts < max_attempts
                ORDER BY requested_at LIMIT 50
            ''')
        rows = cur.fetchall()
        selected = None
        for run_id, task_name in rows:
            decision = _queue_dependency_decision(task_name, cur)
            if decision['state'] == 'blocked':
                details = '; '.join(
                    f"{item['dependency']}={item.get('reason')}"
                    for item in decision['details'])
                cur.execute('''
                    UPDATE manual_task_runs
                    SET status = 'skipped', finished_at = ?,
                        error = ?, worker_id = NULL, heartbeat_at = NULL
                    WHERE run_id = ? AND status = 'queued'
                ''', (_now(), f'dependency_failed: {details}'[:1000], run_id))
                continue
            if decision['state'] == 'ready':
                selected = run_id
                break
        if not selected:
            conn.commit()
            conn.close()
            return None
        run_id = selected
        cur.execute('''
            UPDATE manual_task_runs
            SET status = 'running', started_at = ?, heartbeat_at = ?,
                worker_id = ?, attempts = attempts + 1, error = NULL
            WHERE run_id = ? AND status = 'queued'
        ''', (now, now, worker_id, run_id))
        if cur.rowcount != 1:
            conn.rollback()
            conn.close()
            return None
        conn.commit()
        conn.close()
        return get_task_run(run_id)
    except Exception:
        conn.rollback()
        conn.close()
        raise


def _scheduled_task_running(task_name: str) -> bool:
    """读取同进程 jobs_hub 的运行表；不可用时由数据库状态继续判断。"""
    for module_name in ('jobs.jobs_hub', 'jobs_hub'):
        module = sys.modules.get(module_name)
        if module is not None and task_name in getattr(module, '_TASK_START_TS', {}):
            return True
    return False


def _queue_dependency_decision(task_name: str, cur) -> Dict[str, Any]:
    dependencies = _task_dependencies(task_name)
    states = []
    start, end = _today_bounds()
    for dependency in dependencies:
        if not _dependency_enabled(dependency):
            states.append({'state': 'ready', 'dependency': dependency, 'reason': 'disabled'})
            continue
        cur.execute('''
            SELECT status, finished_at, error FROM job_runs
            WHERE job_name = ? AND started_at >= ? AND started_at < ?
            ORDER BY id DESC
        ''', (dependency, start, end))
        scheduled = cur.fetchall()
        if any(str(row[0]) == 'success' for row in scheduled):
            states.append({'state': 'ready', 'dependency': dependency, 'reason': 'success'})
            continue
        cur.execute('''
            SELECT status FROM manual_task_runs
            WHERE task_name = ? AND status IN ('queued', 'running')
            ORDER BY requested_at DESC LIMIT 1
        ''', (dependency,))
        active = cur.fetchone()
        if active or _scheduled_task_running(dependency):
            states.append({'state': 'pending', 'dependency': dependency, 'reason': 'active'})
            continue
        if scheduled:
            states.append({
                'state': 'blocked', 'dependency': dependency,
                'reason': str(scheduled[0][0]), 'error': scheduled[0][2],
            })
        else:
            states.append({'state': 'pending', 'dependency': dependency,
                           'reason': 'not_started'})
    blocked = [item for item in states if item['state'] == 'blocked']
    pending = [item for item in states if item['state'] == 'pending']
    return {
        'state': 'blocked' if blocked else 'pending' if pending else 'ready',
        'details': blocked or pending or states,
    }


def _heartbeat_loop(run_id: str, stop: threading.Event, interval: int = 30) -> None:
    while not stop.wait(interval):
        try:
            conn = db_connect(_DB_PATH)
            cur = conn.cursor()
            cur.execute('''
                UPDATE manual_task_runs SET heartbeat_at = ?
                WHERE run_id = ? AND status = 'running'
            ''', (_now(), run_id))
            conn.commit()
            conn.close()
        except Exception:
            pass


def requeue_claimed_task(run_id: str, reason: str) -> None:
    """jobs_hub 领取后若提交线程池失败，释放回队列或在耗尽时终止。"""
    _init_db()
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        UPDATE manual_task_runs
        SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'interrupted' END,
            worker_id = NULL, heartbeat_at = NULL,
            started_at = NULL, error = ?
            , finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE ? END
        WHERE run_id = ? AND status = 'running'
    ''', ((reason or 'worker submit failed')[:1000], _now(), run_id))
    conn.commit()
    conn.close()


def execute_claimed_task(run: Dict[str, Any], task_runner=None) -> None:
    """由 jobs_hub worker 执行已领取任务并回写最终状态。

    task_runner 由 jobs_hub 传入 `_run_with_log`，复用任务级硬超时和告警；
    留空主要供独立测试，直接调用任务函数。
    """
    run_id = run['run_id']
    task_name = run['task_name']
    started_at = str(run.get('started_at') or _now())
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(run_id, stop),
        name=f'task-heartbeat-{run_id[:8]}',
        daemon=True,
    )
    heartbeat.start()
    try:
        fn = _resolve_task(task_name)
        value = task_runner(task_name, fn) if task_runner else fn()
        job_status, job_error = _latest_job_outcome(task_name, started_at)
        final_status = {
            'error': 'error',
            'failed': 'error',
            'timeout': 'timeout',
            'skipped': 'skipped',
            'degraded': 'degraded',
            'partial': 'partial',
        }.get(job_status, 'success')
        result = {'job_status': job_status}
        if value is not None:
            result['return_value'] = value
        _update_run(
            run_id,
            status=final_status,
            finished_at=_now(),
            result=result,
            error=job_error,
            heartbeat_at=_now(),
        )
    except Exception as exc:
        try:
            _update_run(
                run_id,
                status='error',
                finished_at=_now(),
                error=f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-4000:]}',
                heartbeat_at=_now(),
            )
        except Exception:
            pass
    finally:
        stop.set()


def recent_scheduled_runs(limit: int = 200) -> List[Dict[str, Any]]:
    """读取 jobs_hub 的最近运行记录，不触发任何外部数据请求。"""
    limit = max(1, min(int(limit), 1000))
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT job_name, started_at, finished_at, status, error
            FROM job_runs ORDER BY id DESC LIMIT ?
        ''', (limit,))
        rows = cur.fetchall()
        conn.close()
        return [
            {
                'job_name': row[0], 'started_at': row[1], 'finished_at': row[2],
                'status': row[3], 'error': row[4], 'kind': 'scheduled',
            }
            for row in rows
        ]
    except Exception:
        return []


def enrich_task_catalog(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给任务清单附上最近调度/手动运行状态，供 MCP 和 Web 共用。"""
    scheduled_latest: Dict[str, Dict[str, Any]] = {}
    for run in recent_scheduled_runs(max(200, len(tasks) * 4)):
        scheduled_latest.setdefault(run['job_name'], run)
    manual_latest: Dict[str, Dict[str, Any]] = {}
    try:
        for run in list_task_runs(limit=max(100, len(tasks) * 3)):
            manual_latest.setdefault(run['task_name'], run)
    except Exception:
        pass

    out = []
    for task in tasks:
        item = dict(task)
        item['last_run'] = scheduled_latest.get(item.get('name'))
        item['manual_run'] = manual_latest.get(item.get('name'))
        try:
            _resolve_task(item.get('name', ''))
            item['triggerable'] = True
            item['trigger_note'] = ''
        except Exception as exc:
            item['triggerable'] = False
            item['trigger_note'] = str(exc)
        out.append(item)
    return out


def effective_task_run(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """取调度/手动运行中时间更新的一条；排队或运行中的手动任务优先。"""
    manual = task.get('manual_run')
    scheduled = task.get('last_run')
    if manual and manual.get('status') in ('queued', 'running'):
        return manual
    manual_at = manual and (
        manual.get('finished_at') or manual.get('started_at') or manual.get('requested_at'))
    scheduled_at = scheduled and (scheduled.get('finished_at') or scheduled.get('started_at'))
    if manual_at and (not scheduled_at or str(manual_at) >= str(scheduled_at)):
        return manual
    return scheduled


def latest_selection_artifact() -> Dict[str, Any]:
    """Read the single authoritative formal selection and optional display overlay.

    ``selection_artifacts`` is the source of truth.  The legacy indicator snapshot
    is consulted only for presentation fields and can never supply or reorder the
    formal candidates.
    """
    from agent_contract import envelope
    from data.research_store import ResearchStore

    formal = ResearchStore(ensure_schema=False).latest_formal_selection()
    if not formal:
        return envelope(
            {'picks': [], 'rows': [], 'final_picks': [], 'final_rows': []},
            status='missing', warnings=['尚无正式综合选股产物'],
            sources=['selection_artifacts'], artifact_type='unified_selection',
            as_of=None)

    artifacts = formal.get('artifacts') or {}
    top15 = (artifacts.get('formal_top15') or {}).get('payload') or []
    top5 = (artifacts.get('formal_top5') or {}).get('payload') or []
    overlay = (artifacts.get('display_overlay') or {}).get('payload') or {}
    external = (artifacts.get('external_reference') or {}).get('payload') or {}
    local_strategy_reference = (
        (artifacts.get('local_strategy_nominations') or {}).get('payload')
        or (artifacts.get('local_strategy_reference') or {}).get('payload') or {}
    )
    genome_nominations = (
        (artifacts.get('genome_nominations') or {}).get('payload') or {}
    )
    fusion_policy = (artifacts.get('fusion_policy') or {}).get('payload') or {}
    wencai_strategy_runs = (
        (artifacts.get('wencai_strategy_runs') or {}).get('payload') or {}
    )
    miaoxiang_review = (
        (artifacts.get('miaoxiang_review') or {}).get('payload') or {}
    )
    miaoxiang_strategy_runs = (
        (artifacts.get('miaoxiang_strategy_runs') or {}).get('payload') or {}
    )
    ai_review = (artifacts.get('ai_review') or {}).get('payload') or {}
    metadata = formal.get('metadata') or {}
    overlay_rows = overlay if isinstance(overlay, list) else []
    review_rows = ai_review if isinstance(ai_review, list) else []
    overlay_by_code = {
        str(row.get('code') or row.get('symbol') or ''): row
        for row in overlay_rows if isinstance(row, dict)
    }
    review_by_code = {
        str(row.get('code') or row.get('symbol') or ''): row
        for row in review_rows if isinstance(row, dict)
    }

    def decorate(rows):
        decorated = []
        for formal_row in rows:
            code = str(formal_row.get('code') or formal_row.get('symbol') or '')
            # Formal membership/rank fields win over optional display attachments.
            decorated.append({
                **overlay_by_code.get(code, {}),
                **review_by_code.get(code, {}),
                **formal_row,
            })
        return decorated

    top15_display = decorate(top15)
    top5_display = decorate(top5)

    data = {
        'run_id': formal.get('run_id'),
        'snapshot_id': metadata.get('snapshot_id'),
        'manifest_id': metadata.get('manifest_id'),
        'rule_version': metadata.get('rule_version'),
        'policy_hash': metadata.get('policy_hash'),
        'selection_date': formal.get('selection_date'),
        'market_as_of': metadata.get('market_as_of'),
        'picks': [str(row.get('symbol')) for row in top15 if row.get('symbol')],
        'rows': top15_display,
        'final_picks': [str(row.get('symbol')) for row in top5 if row.get('symbol')],
        'final_rows': top5_display,
        'display_overlay': overlay,
        'external_reference': external,
        'local_strategy_reference': local_strategy_reference,
        'local_strategy_nominations': local_strategy_reference,
        'genome_nominations': genome_nominations,
        'fusion_policy': fusion_policy,
        'lane_counts': metadata.get('lane_counts') or {},
        'wencai_strategy_runs': wencai_strategy_runs,
        'miaoxiang_strategy_runs': miaoxiang_strategy_runs,
        'miaoxiang_review': miaoxiang_review,
        'ai_review': ai_review,
        'artifacts': {
            name: {
                key: value for key, value in artifact.items() if key != 'payload'
            }
            for name, artifact in artifacts.items()
        },
    }

    warnings = []
    required = ('formal_top15', 'formal_top5')
    missing = [name for name in required if name not in artifacts]
    if missing:
        warnings.append('正式产物不完整: ' + ','.join(missing))
    snapshot_date = str(formal.get('selection_date') or '')
    today_date = datetime.now().astimezone().date()
    expected_weekend_snapshot = False
    try:
        snapshot_day = datetime.strptime(snapshot_date[:10], '%Y-%m-%d').date()
        expected_weekend_snapshot = (today_date.weekday() >= 5
                                     and snapshot_day.weekday() == 4
                                     and 1 <= (today_date - snapshot_day).days <= 2)
    except Exception:
        pass
    if snapshot_date != today_date.isoformat() and not expected_weekend_snapshot:
        warnings.append(f'最近正式产物来自 {snapshot_date}，不是今天')
    return envelope(
        data,
        status=('success' if not warnings else
                'failed' if missing else 'stale'),
        warnings=warnings,
        sources=['selection_runs', 'selection_artifacts', 'selection_input_manifests'],
        artifact_type='unified_selection',
        snapshot_date=snapshot_date,
        as_of=metadata.get('decision_at') or snapshot_date,
    )


def agent_cockpit(recent_limit: int = 5, compact: bool = True) -> Dict[str, Any]:
    """Agent 的只读总览：任务健康、选股产物、持仓/推荐/信号数量和数据源状态。"""
    from agent_contract import envelope
    warnings: List[str] = []
    data: Dict[str, Any] = {}

    try:
        from automation_config import list_all
        jobs = enrich_task_catalog(list_all())
        failed = []
        disabled_core = []
        for job in jobs:
            last = effective_task_run(job)
            if last and last.get('status') in (
                    'error', 'failed', 'timeout', 'interrupted', 'degraded', 'partial'):
                failed.append({
                    'name': job['name'], 'status': last.get('status'),
                    'error': last.get('error'), 'at': last.get('finished_at') or last.get('started_at'),
                })
            if job.get('core') and not job.get('enabled'):
                disabled_core.append(job['name'])
        data['tasks'] = {
            'total': len(jobs),
            'failed_recent': failed[:recent_limit],
            'disabled_core': disabled_core,
            'running_manual': [
                j['manual_run'] for j in jobs
                if j.get('manual_run') and j['manual_run'].get('status') in ('queued', 'running')
            ],
        }
        if failed:
            warnings.append(f'{len(failed)} 个任务最近一次运行失败')
        if disabled_core:
            warnings.append(f'{len(disabled_core)} 个核心任务已关闭')
    except Exception as exc:
        warnings.append(f'任务状态读取失败: {exc}')

    selection = latest_selection_artifact()
    if compact and isinstance(selection.get('data'), dict):
        selection = dict(selection)
        selection['data'] = dict(selection['data'])
        for key, value in list(selection['data'].items()):
            if isinstance(value, list) and len(value) > 8:
                selection['data'][key] = value[:8]
                selection['data'][f'{key}_total'] = len(value)
    data['selection'] = selection
    warnings.extend(selection.get('meta', {}).get('warnings') or [])

    try:
        from portfolio_db import portfolio_db
        data['holding_count'] = len(portfolio_db.get_all_stocks() or [])
    except Exception as exc:
        data['holding_count'] = None
        warnings.append(f'持仓读取失败: {exc}')

    try:
        from ai_recommendation_monitor import list_active
        active = list_active(limit=recent_limit)
        data['active_recommendations'] = active
        data['active_recommendation_count'] = len(active)
    except Exception as exc:
        data['active_recommendations'] = []
        data['active_recommendation_count'] = None
        warnings.append(f'推荐池读取失败: {exc}')

    try:
        from decision_signal import list_signals
        signals = list_signals(status='active', limit=recent_limit)
        data['active_signals'] = signals
        data['active_signal_count'] = len(signals)
    except Exception as exc:
        data['active_signals'] = []
        data['active_signal_count'] = None
        warnings.append(f'决策信号读取失败: {exc}')

    try:
        import datahub
        data['datahub'] = {
            'sources': datahub.source_stats(),
            'cache': datahub.cache_stats(),
        }
    except Exception as exc:
        data['datahub'] = None
        warnings.append(f'数据层健康度读取失败: {exc}')

    try:
        from portfolio_policy import status as portfolio_policy_status
        data['portfolio_policy'] = portfolio_policy_status()
        if (data['portfolio_policy'].get('fail_closed')
                and datetime.now().astimezone().weekday() < 5):
            warnings.append('高仓位模式下尚无今日加仓判断，自动买入已按保守规则关闭')
    except Exception as exc:
        data['portfolio_policy'] = None
        warnings.append(f'仓位策略读取失败: {exc}')

    try:
        from llm_usage import summary as llm_usage_summary
        data['llm_telemetry'] = llm_usage_summary(days=7).get('totals')
    except Exception as exc:
        data['llm_telemetry'] = None
        warnings.append(f'LLM 遥测读取失败: {exc}')

    try:
        from analysis.strategy_genome import get_live_strategy_set
        live = get_live_strategy_set() or {}
        meta = live.get('base_meta') or {}
        data['strategy_deployment'] = {
            'base_total': len(live.get('base') or {}),
            'evolved_base': sum(1 for row in meta.values()
                                if (row.get('generation') or 0) > 0),
            'default_fallback': [sid for sid, row in meta.items()
                                 if (row.get('generation') or 0) == 0],
            'composed': live.get('composed') or [],
        }
    except Exception as exc:
        data['strategy_deployment'] = None
        warnings.append(f'策略部署集读取失败: {exc}')

    return envelope(
        data,
        status='degraded' if warnings else 'success',
        warnings=warnings,
        sources=['job_runs', 'manual_task_runs', 'indicator_snapshots',
                 'portfolio', 'ai_recommendations', 'decision_signals', 'datahub',
                 'strategy_variants'],
    )
