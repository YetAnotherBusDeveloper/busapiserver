from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from app.request_budget import budget_lock, request_budget, request_timeout
from app.tdx_client import TDXClient


class RequestBudgetTests(unittest.TestCase):
    def test_retry_after_exceeding_budget_is_not_slept_or_retried(self):
        settings = SimpleNamespace(tdx_base_url="https://test.invalid", tdx_retry_attempts=6,
                                   tdx_min_request_interval=0, tdx_request_timeout=30)
        client = TDXClient(settings, Mock())
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "60"
        try:
            with patch.object(client._session, "get", return_value=response) as fetch, \
                 patch("app.request_budget.time.sleep") as sleep, request_budget(10):
                with self.assertRaises(requests.Timeout):
                    client._request_json("/resource")
                fetch.assert_called_once()
                sleep.assert_not_called()
                self.assertLessEqual(fetch.call_args.kwargs["timeout"].total, 10)
        finally:
            client.close()

    def test_lock_wait_has_a_deadline(self):
        lock = threading.Lock()
        lock.acquire()
        try:
            with request_budget(0.02), self.assertRaises(requests.Timeout):
                with budget_lock(lock):
                    self.fail("must not enter a held lock")
        finally:
            lock.release()

    def test_nested_resources_share_budget_and_static_sync_keeps_normal_timeout(self):
        with patch("app.request_budget.time.monotonic", return_value=100):
            with request_budget(10):
                with patch("app.request_budget.time.monotonic", return_value=108):
                    with request_budget(10):
                        self.assertEqual(request_timeout(30).total, 2)
        self.assertEqual(request_timeout(30), 30)
