from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from app.runtime_security import require_safe_brotli


class BrotliRuntimeSecurityTests(unittest.TestCase):
    def check_versions(self, packages: dict[str, str]) -> None:
        def installed_version(package: str) -> str:
            if package not in packages:
                raise PackageNotFoundError(package)
            return packages[package]

        with patch("app.runtime_security.version", side_effect=installed_version):
            require_safe_brotli()

    def test_patched_bindings_are_accepted(self) -> None:
        for packages in (
            {"Brotli": "1.2.1"},
            {"brotlicffi": "1.2.1.0"},
            {"Brotli": "1.2.2", "brotlicffi": "1.2.1.1"},
        ):
            with self.subTest(packages=packages):
                self.check_versions(packages)

    def test_old_native_decoder_is_rejected(self) -> None:
        for installed in ("1.0.9", "1.1.0", "1.2.0", "1.2.1rc1", "unknown"):
            with self.subTest(installed=installed):
                with self.assertRaisesRegex(RuntimeError, "CVE-2026-111497"):
                    self.check_versions({"Brotli": installed})

    def test_optional_old_cffi_binding_is_rejected_even_with_safe_brotli(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "brotlicffi.*HTTP2Shell"):
            self.check_versions({"Brotli": "1.2.1", "brotlicffi": "1.2.0.0"})

    def test_missing_optional_bindings_do_not_block_unrelated_tools(self) -> None:
        self.check_versions({})

    def test_app_import_blocks_a_stale_decoder_before_server_startup(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.metadata as metadata\n"
                "metadata.version = lambda name: '1.2.0'\n"
                "import app\n",
            ],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CVE-2026-111497", result.stderr)


if __name__ == "__main__":
    unittest.main()
