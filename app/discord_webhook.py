"""Discord webhook notifications.

These notifications deliberately carry only metadata and a link back to the
admin dashboard. Raw user-submitted text (feedback title/content) is never
included in the webhook payload, so malicious or abusive input cannot reach
Discord and put the server's account at risk. Reviewers open the admin page to
read the actual content in a controlled environment.
"""

from __future__ import annotations

from typing import Any

import requests

from app.config import Settings
from app.logging_utils import get_logger


LOGGER = get_logger("discord_webhook")

_DISCORD_TIMEOUT_SECONDS = 10
# Discord brand-ish blue for the embed sidebar.
_EMBED_COLOR = 0x5865F2


def _post_webhook(webhook_url: str, payload: dict[str, Any]) -> bool:
    try:
        response = requests.post(webhook_url, json=payload, timeout=_DISCORD_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        LOGGER.warning("discord webhook request failed: %s", exc)
        return False

    if not response.ok:
        LOGGER.warning(
            "discord webhook returned non-ok status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return False
    return True


def notify_new_feedback(
    settings: Settings,
    *,
    feedback_id: int,
    account_id: int,
    account_role: str | None,
    created_at: int,
    title_length: int,
    content_length: int,
    client_family: str | None,
    platform_name: str | None,
    app_version: str | None,
) -> bool:
    """Send a metadata-only Discord notification about a new feedback entry.

    Returns True if the webhook was delivered successfully, False otherwise
    (including when no webhook URL is configured). Never raises.
    """
    webhook_url = settings.feedback_discord_webhook_url
    if not webhook_url:
        return False

    admin_url = f"{settings.auth_public_base_url}/admin/feedbacks"

    # NOTE: Only non-sensitive metadata is included here. The raw user-provided
    # title/content are intentionally omitted; we only report their lengths so a
    # reviewer can gauge size before opening the dashboard.
    fields = [
        {"name": "Feedback ID", "value": str(feedback_id), "inline": True},
        {"name": "Account", "value": f"{account_id} ({account_role or 'user'})", "inline": True},
        {"name": "Title length", "value": str(title_length), "inline": True},
        {"name": "Content length", "value": str(content_length), "inline": True},
    ]
    if client_family:
        platform_label = client_family
        if platform_name:
            platform_label = f"{client_family} / {platform_name}"
        if app_version:
            platform_label = f"{platform_label} (v{app_version})"
        fields.append({"name": "Client", "value": platform_label, "inline": True})

    payload = {
        "username": "YABus Feedback",
        "embeds": [
            {
                "title": "📝 New feedback received",
                "description": f"[Open admin dashboard to review]({admin_url})",
                "color": _EMBED_COLOR,
                "fields": fields,
                "timestamp": _iso8601(created_at),
                "footer": {"text": "Content withheld — review in admin dashboard."},
            }
        ],
        # Defense-in-depth: never allow any mentions to ping, even if some field
        # ever contained an @everyone/role-like string.
        "allowed_mentions": {"parse": []},
    }

    return _post_webhook(webhook_url, payload)


_ALERT_COLOR = 0xED4245
_RECOVERED_COLOR = 0x57F287


def notify_realtime_feed(
    settings: Settings,
    *,
    city: str,
    empty_since: int,
    route_count: int,
    recovered_at: int | None = None,
) -> bool:
    """Report that a city's realtime ETA feed went empty, or came back.

    ``recovered_at`` of None means the feed is still empty. Returns True if the
    webhook was delivered; never raises.
    """
    webhook_url = settings.realtime_alert_discord_webhook_url or settings.feedback_discord_webhook_url
    if not webhook_url:
        return False

    if recovered_at is None:
        embed = {
            "title": f"⚠️ Realtime feed empty: {city}",
            "description": (
                f"Every ETA refresh for {city} has produced no rows for any route since "
                f"<t:{empty_since}:R>, so arrival times are blank for users. Query TDX "
                "EstimatedTimeOfArrival for the city without a $filter: 0 items there means "
                "the upstream feed is empty; otherwise our route filter or mapping no longer matches."
            ),
            "color": _ALERT_COLOR,
            "fields": [
                {"name": "City", "value": city, "inline": True},
                {"name": "Empty since", "value": f"<t:{empty_since}:f>", "inline": True},
                {"name": "Routes refreshed", "value": str(route_count), "inline": True},
            ],
            "timestamp": _iso8601(empty_since),
        }
    else:
        embed = {
            "title": f"✅ Realtime feed recovered: {city}",
            "description": f"ETA data for {city} is back after {_format_duration(recovered_at - empty_since)}.",
            "color": _RECOVERED_COLOR,
            "fields": [
                {"name": "City", "value": city, "inline": True},
                {"name": "Empty since", "value": f"<t:{empty_since}:f>", "inline": True},
                {"name": "Recovered", "value": f"<t:{recovered_at}:f>", "inline": True},
            ],
            "timestamp": _iso8601(recovered_at),
        }

    payload = {
        "username": "YABus Monitor",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    return _post_webhook(webhook_url, payload)


def _format_duration(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _iso8601(epoch_seconds: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
