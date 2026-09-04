"""Legacy API backed by a single host-wide provider admission boundary."""
from functools import wraps
import time
from data.provider_governor import (
    PROVIDERS, guarded_session, host_provider, operational_interval, provider_slot,
)


def _fold_host(host):
    return host if host in PROVIDERS else host_provider("https://" + host)


def host_key(url):
    return host_provider(url)


def _gap_for(source):
    return operational_interval(_fold_host(source))


def throttle(source="default", min_interval=None):
    started = time.monotonic()
    with provider_slot(_fold_host(source), interval=min_interval or 0, charge=False):
        pass
    return time.monotonic() - started


def throttled_session(session, key=None):
    return guarded_session(session, key)


def throttled(key="default"):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with provider_slot(_fold_host(key)):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
