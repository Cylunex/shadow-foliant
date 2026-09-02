"""Bound BaoStock SDK socket I/O without changing process-wide socket defaults.

Only enter while holding baostock._provider_slot: the SDK has global connection
state. Its stock socketutil otherwise has no timeout and loops forever on EOF.
"""
from contextlib import contextmanager
import time


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.original_timeout = sock.gettimeout()

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def _call(self, method, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("baostock request deadline exceeded")
        self.sock.settimeout(remaining)
        result = getattr(self.sock, method)(*args)
        if method == "recv" and not result:
            raise ConnectionError("baostock connection closed")
        return result

    def connect(self, *args):
        return self._call("connect", *args)

    def send(self, *args):
        return self._call("send", *args)

    def recv(self, *args):
        return self._call("recv", *args)


@contextmanager
def sdk_deadline(seconds=15):
    import baostock.util.socketutil as util
    import baostock.common.context as context

    deadline = time.monotonic() + max(.1, seconds)
    original_module = util.socket

    class SocketModule:
        def __getattr__(self, name):
            return getattr(original_module, name)

        def socket(self, *args, **kwargs):
            return DeadlineSocket(original_module.socket(*args, **kwargs), deadline)

    existing = getattr(context, "default_socket", None)
    if existing is not None:
        context.default_socket = DeadlineSocket(existing, deadline)
    util.socket = SocketModule()
    try:
        yield
    finally:
        util.socket = original_module
        current = getattr(context, "default_socket", None)
        if isinstance(current, DeadlineSocket):
            context.default_socket = current.sock
            try:
                current.sock.settimeout(current.original_timeout)
            except OSError:
                pass
