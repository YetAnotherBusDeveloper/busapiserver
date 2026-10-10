"""A shared upstream deadline across throttling, locks, retries and resources.

ContextVars propagate from ASGI into FastAPI's worker pool. Static sync outside
request handling retains its existing long-running retry policy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time

import requests
from urllib3.util import Timeout

_deadline: ContextVar[float | None] = ContextVar("upstream_deadline", default=None)


def remaining_seconds() -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout("Upstream request budget exhausted")
    return remaining


@contextmanager
def request_budget(seconds: float):
    previous = _deadline.get()
    deadline = time.monotonic() + seconds
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def realtime_budget(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with request_budget(getattr(self.settings, "realtime_request_budget_seconds", 10)):
            return function(self, *args, **kwargs)
    return wrapped


def request_timeout(default: float):
    remaining = remaining_seconds()
    if remaining is None:
        return default
    total = min(default, remaining)
    return Timeout(total=total, connect=min(3.0, total), read=total)


def budget_sleep(seconds: float) -> None:
    remaining = remaining_seconds()
    if remaining is not None and seconds >= remaining:
        raise requests.Timeout("Upstream retry exceeds request budget")
    time.sleep(seconds)


@contextmanager
def budget_lock(lock):
    remaining = remaining_seconds()
    acquired = lock.acquire() if remaining is None else lock.acquire(timeout=remaining)
    if not acquired:
        raise requests.Timeout("Upstream lock wait exceeds request budget")
    try:
        remaining_seconds()
        yield
    finally:
        lock.release()
