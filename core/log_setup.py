# -*- coding: utf-8 -*-
"""统一日志初始化 —— 给所有 print() 自动加时间戳, 同时提供 logging 接口。

设计目标:
  1. **零侵入**:项目历史 ~6 万行代码用的是裸 print, 不要求改任何调用方;
     在 sys.stdout/stderr 外面套一层透明的"行拦截器", 每行前自动 prepend
     `YYYY-MM-DD HH:MM:SS`。
  2. **不重复**:行首已有 ISO 时间戳(uvicorn / python logging / 其它工具自带的)
     不二次加。
  3. **不破坏 tqdm/进度条**:tqdm 用 `\r` 刷新, 不是 `\n`;只在遇到完整 newline
     时介入, 进度条的 partial line 不被打扰。
  4. **可选 logger**:新代码用 `get_logger(name)` 走 stdlib logging, 格式与
     stdout 拦截器对齐, 方便统一 grep。

调用方式:
    # _bootstrap.py 末尾(项目所有入口都过):
    from core.log_setup import init_logging
    init_logging()

    # 任意新代码:
    from core.log_setup import get_logger
    log = get_logger(__name__)
    log.info('xxx')

环境变量:
    SHADOW_LOG_TIMESTAMPS=false  关闭 stdout 时间戳拦截(supervisor 不需要重复加时也用)
    SHADOW_LOG_LEVEL=INFO        get_logger 默认级别(DEBUG/INFO/WARNING/ERROR)
    SHADOW_LOG_FILTER_HTML=false 关闭 HTML 标签行过滤(默认开;过滤 akshare/PyExecJS
                                  上游残留 print 出来的整页 HTML, 实测占日志体积 73%)
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from datetime import datetime

# 行首已有时间戳的样式:
#   2026-06-15 14:23:45
#   2026-06-15T14:23:45
#   INFO:     ... (uvicorn 默认风格)— 不加 ts, 因为 uvicorn 自己 log_config 里已 prepend ts
_TS_OK_RE = re.compile(
    r'^('
    r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'    # ISO 时间戳
    r'|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+'  # logging 默认风格(带 ,毫秒)
    r')'
)

# 调试用前缀,这些已经是 uvicorn 自带带 ts 的 INFO:/WARNING:/ERROR: 行 → 不二次加
_UVICORN_RE = re.compile(r'^(INFO|WARNING|ERROR|DEBUG|CRITICAL):\s')

# HTML 标签行特征:trim 后头是 '<' 且 (含 '</' 或 ' class=' 或 '/>' 或常见 HTML 标签名)
# 误杀风险评估:业务 print 极少这样开头, 即便有 `<dict>` `<obj>` 也不会含 'class=' '</' '/>',
# 所以触发器组合起来很安全。下面"任一命中即视为 HTML 噪音"。
_HTML_HINTS = ('</', 'class=', 'style=', '/>',
               '<td', '<tr', '<div', '<th', '<span', '<br',
               '<!DOCTYPE', '<!doctype', '<html', '<table',
               '<thead', '<tbody', '<a ', '<h1', '<h2', '<h3', '<h4',
               '<script', '<link', '<meta', '<input', '<button')


def _is_html_noise(line: str) -> bool:
    """识别"akshare/PyExecJS 上游残留 print"出来的整页 HTML 标签行。
    保守判断:头是 '<', 并且含 HTML 标志符号之一。这样业务里写 `print('<msg>')`
    类的不会误伤(没有 HTML 标志)。"""
    s = line.lstrip()
    if not s or s[0] != '<':
        return False
    return any(h in s for h in _HTML_HINTS)


# Node 子进程 deprecation 警告 — akshare→PyExecJS 每开一个 node 都打一遍, 占大量噪音。
# _bootstrap.py 已经设 NODE_OPTIONS=--no-deprecation 让 node 不发, 这里再加一道兜底
# 过滤(防止有未走 NODE_OPTIONS 的旁路)。
# 形如:
#   (node:12345) [DEP0040] DeprecationWarning: The `punycode` module is deprecated. ...
#   (Use `node --trace-deprecation ...` to show where the warning was created)
_NODE_DEP_RE = re.compile(r'^\(node:\d+\)\s*\[DEP\d+\]|^\(Use `node --trace-deprecation')


def _is_node_dep_noise(line: str) -> bool:
    return bool(_NODE_DEP_RE.match(line.lstrip()))


# 过滤统计:get_filter_stats() 可以观测
_FILTER_STATS = {'html_dropped': 0, 'node_dep_dropped': 0}


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class _TimestampedStream:
    """sys.stdout/stderr 的薄包装:每个完整 '\\n' 结尾的行前 prepend 时间戳。

    实现注意:
      - **按行处理**:write() 不保证以完整行调用, 用内部 buffer 拼到 '\\n' 再切;
      - **保留 '\\r'**:tqdm 之类靠 '\\r' 刷新进度, 不加 ts(否则进度条会糊);
      - **空行不加 ts**;
      - **已有 ts 的行不二次加**(uvicorn / logging / 上游已有 ts 的来源);
      - **线程安全**:并发 print 间加锁, 防止行交错;
      - **透传其它属性**(isatty/fileno/encoding/...)给底层流。
    """

    def __init__(self, target, filter_html: bool = True):
        self._target = target
        self._buf = ''
        self._lock = threading.Lock()
        self._filter_html = filter_html

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._buf += s
            if '\n' not in self._buf:
                # 没遇到完整行(可能是 tqdm 的 \r 刷新, 或 partial line), 不处理
                return len(s)
            # 切出已完整的行, 最后一段没 \n 的留下次
            *complete, self._buf = self._buf.split('\n')
            now = _ts()
            out_lines = []
            for line in complete:
                # HTML 噪音过滤(默认开):akshare/PyExecJS 上游残留 print 的
                # 整页 HTML, 实测占日志体积 73%, 直接 drop
                if self._filter_html and _is_html_noise(line):
                    _FILTER_STATS['html_dropped'] += 1
                    continue
                # Node 子进程 punycode/DEP 警告(无价值, 上游 npm 依赖问题)
                if self._filter_html and _is_node_dep_noise(line):
                    _FILTER_STATS['node_dep_dropped'] += 1
                    continue
                if not line.strip():
                    out_lines.append(line)
                elif _TS_OK_RE.match(line) or _UVICORN_RE.match(line):
                    out_lines.append(line)
                else:
                    # 行尾可能有 '\r'(tqdm finalize), 时间戳放最前
                    out_lines.append(f'{now} {line}')
            if out_lines:
                self._target.write('\n'.join(out_lines) + '\n')
            return len(s)

    def flush(self):
        # 只 flush 底层, 不强行把 partial buffer 输出 + 加 ts
        # (那会打断 tqdm 进度条;真正的 partial 残留进程退出时也无关紧要)
        with self._lock:
            self._target.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    # 其它属性透传给底层流(isatty / fileno / encoding / errors / closed ...)
    def __getattr__(self, name):
        return getattr(self._target, name)


_installed_lock = threading.Lock()
_installed = False
_orig_stdout = None
_orig_stderr = None


def init_logging():
    """安装 stdout/stderr 时间戳拦截(幂等);并把 stdlib logging 默认级别和格式对齐。

    SHADOW_LOG_TIMESTAMPS=false → 跳过 stdout 拦截(只配 stdlib logging)。
    """
    global _installed, _orig_stdout, _orig_stderr
    with _installed_lock:
        if _installed:
            return
        _installed = True

        # —— stdout / stderr 拦截 —— (可关)
        if os.getenv('SHADOW_LOG_TIMESTAMPS', 'true').lower() not in ('false', '0', 'no'):
            _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
            filter_html = os.getenv('SHADOW_LOG_FILTER_HTML', 'true').lower() not in ('false', '0', 'no')
            sys.stdout = _TimestampedStream(sys.stdout, filter_html=filter_html)
            sys.stderr = _TimestampedStream(sys.stderr, filter_html=filter_html)

        # —— stdlib logging 默认 root 配置(给 get_logger 用, 也兜底第三方库) ——
        level_str = os.getenv('SHADOW_LOG_LEVEL', 'INFO').upper()
        level = getattr(logging, level_str, logging.INFO)
        root = logging.getLogger()
        # 避免重复 attach(test/重载场景)
        for h in list(root.handlers):
            if getattr(h, '_shadow_handler', False):
                root.removeHandler(h)
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            fmt='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        h._shadow_handler = True  # type: ignore[attr-defined]
        root.addHandler(h)
        if root.level == logging.WARNING:  # python 默认 WARNING, 调到 INFO
            root.setLevel(level)


def restore():
    """卸载 stdout 拦截(测试/调试用,生产不需要)。"""
    global _installed
    if _orig_stdout is not None:
        sys.stdout = _orig_stdout
    if _orig_stderr is not None:
        sys.stderr = _orig_stderr
    _installed = False


def get_filter_stats() -> dict:
    """返回 stdout 拦截器过滤掉的行计数(观测用)。"""
    return dict(_FILTER_STATS)


def get_logger(name: str = 'shadow') -> logging.Logger:
    """获取 logger。新代码推荐用这个替代 print。
    格式: `2026-06-15 14:23:45 [INFO] <name>: <msg>`,与 stdout 拦截器一致。
    """
    # 确保 init_logging 跑过(给那些不经过 _bootstrap 的 unit test 用)
    if not _installed:
        init_logging()
    return logging.getLogger(name)
