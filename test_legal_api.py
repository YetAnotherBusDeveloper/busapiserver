from __future__ import annotations

from fastapi.responses import FileResponse
from pathlib import Path
import unittest

from app.api.legal import (
    get_privacy_policy,
    get_terms_of_service,
    privacy_policy_page,
    terms_of_service_page,
)
from app.main import app


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "app" / "web"


class LegalApiTests(unittest.TestCase):
    def test_main_app_registers_legal_routes(self) -> None:
        for name, path in (
            ("get_terms_of_service", "/api/v1/terms-of-service"),
            ("get_privacy_policy", "/api/v1/privacy-policy"),
            ("terms_of_service_page", "/terms-of-service"),
            ("privacy_policy_page", "/privacy-policy"),
        ):
            self.assertEqual(str(app.url_path_for(name)), path)

    def test_terms_of_service_endpoint_returns_file_content(self) -> None:
        self.assertEqual(
            get_terms_of_service(),
            {"content": (PROJECT_ROOT / "TERMS.md").read_text(encoding="utf-8")},
        )

    def test_privacy_policy_endpoint_returns_file_content(self) -> None:
        self.assertEqual(
            get_privacy_policy(),
            {"content": (PROJECT_ROOT / "PRIVACY.md").read_text(encoding="utf-8")},
        )

    def test_terms_of_service_page_returns_html_file(self) -> None:
        response = terms_of_service_page()

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path), WEB_ROOT / "terms-of-service.html")

    def test_privacy_policy_page_returns_html_file(self) -> None:
        response = privacy_policy_page()

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path), WEB_ROOT / "privacy-policy.html")


if __name__ == "__main__":
    unittest.main()
