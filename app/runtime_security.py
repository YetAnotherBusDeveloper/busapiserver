"""Reject stale native Brotli decoders before starting any application code."""

from importlib.metadata import PackageNotFoundError, version

from packaging.version import InvalidVersion, Version


def require_safe_brotli() -> None:
    for package, minimum in (("Brotli", "1.2.1"), ("brotlicffi", "1.2.1.0")):
        try:
            installed = version(package)
        except PackageNotFoundError:
            continue

        try:
            safe = Version(installed) >= Version(minimum)
        except InvalidVersion:
            safe = False

        if not safe:
            raise RuntimeError(
                f"{package} {installed} is unsafe for CVE-2026-111497 (HTTP2Shell). "
                f'Run python -m pip install --upgrade "{package}>={minimum},<2.0" '
                "in the server environment, reinstall requirements.txt, and "
                "restart every server process."
            )
