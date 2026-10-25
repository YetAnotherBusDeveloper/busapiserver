"""Minimal bus API server package."""

from app.runtime_security import require_safe_brotli

# Check before HTTP clients import either native decoder, including an optional
# brotlicffi installation that urllib3 prefers even on CPython.
require_safe_brotli()
