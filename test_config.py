from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_realtime_cache_default_covers_frontend_poll_interval(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.config.load_dotenv"
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.realtime_cache_ttl, 15)


if __name__ == "__main__":
    unittest.main()
