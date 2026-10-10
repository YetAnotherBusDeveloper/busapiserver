"""Coalesce overlapping synchronous resource loads in FastAPI's worker pool."""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from functools import wraps
import inspect
import threading

import requests
from app.request_budget import remaining_seconds


def singleflight(function):
    signature = inspect.signature(function, eval_str=True)
    pending: dict[tuple, Future] = {}
    lock = threading.Lock()

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        # Request identity must not split equivalent loads; client identity
        # prevents sharing results across independent applications/tests.
        key = tuple(
            (name, id(value.app.state.tdx_client) if name == "request" else value)
            for name, value in bound.arguments.items()
        )
        with lock:
            future = pending.get(key)
            owner = future is None
            if owner:
                future = pending[key] = Future()
        if not owner:
            try:
                return future.result(timeout=remaining_seconds())
            except FutureTimeout as exc:
                raise requests.Timeout("Shared upstream load exceeds request budget") from exc
        try:
            result = function(*args, **kwargs)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with lock:
                pending.pop(key, None)

    # Keep resolved annotations for FastAPI even when the original module uses
    # postponed annotations; this wrapper lives in a different module.
    wrapped.__signature__ = signature
    return wrapped
