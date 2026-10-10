from __future__ import annotations

import asyncio
from collections import Counter
import threading
import unittest

from fastapi import FastAPI
import httpx

from app.api import bike, metro, rail
from app.rate_limit import reset_rate_limit_state


class _BlockingTdx:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = Counter()

    def fetch_paginated_items(self, path, **kwargs):
        self.calls[path] += 1
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("test upstream was not released")
        return []


class TransitConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_transit_loads_do_not_block_other_requests_and_are_coalesced(self):
        for module, path in (
            (metro, "/api/v1/metro/TRTC/stations"),
            (rail, "/api/v1/tra/stations"),
            (bike, "/api/v1/bike/stations?city=Taipei"),
        ):
            with self.subTest(path=path):
                reset_rate_limit_state()
                module._cache.clear()
                tdx = _BlockingTdx()
                app = FastAPI()
                app.state.tdx_client = tdx
                app.include_router(module.router)

                @app.get("/probe")
                async def probe():
                    return {"ok": True}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    first = asyncio.create_task(client.get(path))
                    second = asyncio.create_task(client.get(path))
                    try:
                        self.assertTrue(await asyncio.to_thread(tdx.started.wait, 1))
                        response = await asyncio.wait_for(client.get("/probe"), 1)
                        self.assertEqual(response.json(), {"ok": True})
                        self.assertFalse(first.done())
                        self.assertFalse(second.done())
                    finally:
                        tdx.release.set()
                    responses = await asyncio.gather(first, second)
                self.assertTrue(all(response.status_code == 200 for response in responses))
                self.assertTrue(all(count == 1 for count in tdx.calls.values()))
                module._cache.clear()

    async def test_failed_load_is_not_cached_and_can_be_retried(self):
        reset_rate_limit_state()
        metro._cache.clear()
        tdx = _BlockingTdx()
        app = FastAPI()
        app.state.tdx_client = tdx
        app.include_router(metro.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            from unittest.mock import patch
            with patch.object(tdx, "fetch_paginated_items", side_effect=RuntimeError("offline")):
                self.assertEqual((await client.get("/api/v1/metro/TRTC/stations")).status_code, 500)
            tdx.release.set()
            self.assertEqual((await client.get("/api/v1/metro/TRTC/stations")).status_code, 200)
        metro._cache.clear()
