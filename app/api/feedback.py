from __future__ import annotations

from pathlib import Path
import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth_service import generate_snowflake
from app.db import get_connection
from app.discord_webhook import notify_new_feedback
from app.logging_utils import get_logger
from app.rate_limit import check_rate_limit, enforce_rate_limit, get_request_principal
from app.request_analytics import MAX_USER_AGENT_LENGTH, parse_user_agent


LOGGER = get_logger("feedback")
router = APIRouter(tags=["feedback"], dependencies=[Depends(enforce_rate_limit)])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
_FEEDBACK_LIMIT_REQUESTS = 1
_FEEDBACK_LIMIT_WINDOW_SECONDS = 60
_FEEDBACK_LIMIT_DETAIL = "Feedback submissions are limited to 1 per minute."


class FeedbackDiagnostics(BaseModel):
    """Explicitly shared app/display context; arbitrary logs are not accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    app_version: str = Field(min_length=1, max_length=100)
    build_number: str = Field(min_length=1, max_length=40)
    commit_hash: str = Field(min_length=1, max_length=64)
    platform: Literal["android", "ios", "macos", "windows", "linux", "fuchsia", "web"]
    locale: str = Field(min_length=1, max_length=35)
    screen_width: int = Field(gt=0, le=100000, strict=True)
    screen_height: int = Field(gt=0, le=100000, strict=True)
    text_scale: float = Field(gt=0, le=10, allow_inf_nan=False)
    brightness: Literal["light", "dark"]


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    category: Literal["bug", "data", "feature", "other"] | None = None
    route_context: str | None = Field(default=None, max_length=200)
    contact: str | None = Field(default=None, max_length=200)
    diagnostics: FeedbackDiagnostics | None = None

    @field_validator("route_context", "contact", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("title", "content")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field must not be empty.")
        return normalized


@router.get("/admin/feedbacks", include_in_schema=False, response_model=None)
def feedbacks_page(request: Request) -> FileResponse | RedirectResponse:
    if get_request_principal(request) is None:
        return RedirectResponse("/auth", status_code=302)
    _require_admin(request)
    return FileResponse(WEB_ROOT / "feedbacks.html", media_type="text/html")


@router.post("/api/v1/feedback", status_code=status.HTTP_201_CREATED)
def create_feedback(request: Request, payload: FeedbackCreateRequest) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    check_rate_limit(
        f"user:{principal.account_id}",
        "feedback-submit",
        requests=_FEEDBACK_LIMIT_REQUESTS,
        window_seconds=_FEEDBACK_LIMIT_WINDOW_SECONDS,
        detail=_FEEDBACK_LIMIT_DETAIL,
    )

    settings = request.app.state.settings
    now = int(time.time())
    feedback_id = generate_snowflake(settings)
    user_agent = (request.headers.get("user-agent") or "").strip()
    parsed_user_agent = parse_user_agent(user_agent)
    safe_user_agent = user_agent[:MAX_USER_AGENT_LENGTH]

    with get_connection(settings.app_db_path) as connection:
        connection.execute(
            """
            INSERT INTO feedbacks (
                id,
                account_id,
                title,
                content,
                created_at,
                client_family,
                platform_name,
                system_name,
                system_version,
                app_version,
                app_commit_hash,
                browser_name,
                browser_version,
                user_agent,
                category,
                route_context,
                contact,
                diagnostics_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                principal.account_id,
                payload.title,
                payload.content,
                now,
                parsed_user_agent.client_family,
                parsed_user_agent.platform_name,
                parsed_user_agent.system_name,
                parsed_user_agent.system_version,
                parsed_user_agent.app_version,
                parsed_user_agent.app_commit_hash,
                parsed_user_agent.browser_name,
                parsed_user_agent.browser_version,
                safe_user_agent,
                payload.category,
                payload.route_context,
                payload.contact,
                payload.diagnostics.model_dump_json() if payload.diagnostics else None,
            ),
        )
        connection.commit()

    # Best-effort Discord notification. Carries only metadata (never the raw
    # user-submitted title/content) and must never break the API response.
    try:
        notify_new_feedback(
            settings,
            feedback_id=feedback_id,
            account_id=principal.account_id,
            account_role=getattr(principal, "role", None),
            created_at=now,
            title_length=len(payload.title),
            content_length=len(payload.content),
            client_family=parsed_user_agent.client_family,
            platform_name=parsed_user_agent.platform_name,
            app_version=parsed_user_agent.app_version,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        LOGGER.warning("failed to send discord feedback notification: %s", exc)

    return {
        "ok": True,
        "feedback_id": str(feedback_id),
        "created_at": now,
    }


@router.get("/api/v1/admin/feedbacks")
def list_feedbacks(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    _require_admin(request)

    now = int(time.time())
    with get_connection(request.app.state.settings.app_db_path) as connection:
        summary_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COUNT(DISTINCT account_id) AS account_count,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent_24h_count,
                MAX(created_at) AS last_created_at
            FROM feedbacks
            """,
            (now - 86400,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT
                f.id,
                f.account_id,
                a.role,
                f.title,
                f.content,
                f.created_at,
                f.client_family,
                f.platform_name,
                f.system_name,
                f.system_version,
                f.app_version,
                f.app_commit_hash,
                f.browser_name,
                f.browser_version,
                f.user_agent,
                f.category,
                f.route_context,
                f.contact,
                f.diagnostics_json,
                (
                    SELECT COALESCE(
                        NULLIF(i.display_name, ''),
                        NULLIF(i.email, ''),
                        i.provider || ':' || i.provider_user_id
                    )
                    FROM account_oauth_identities i
                    WHERE i.account_id = f.account_id
                    ORDER BY
                        CASE i.provider
                            WHEN 'google' THEN 0
                            WHEN 'discord' THEN 1
                            ELSE 2
                        END,
                        i.updated_at DESC,
                        i.created_at DESC
                    LIMIT 1
                ) AS account_label,
                (
                    SELECT COALESCE(NULLIF(i.email, ''), '')
                    FROM account_oauth_identities i
                    WHERE i.account_id = f.account_id
                    ORDER BY
                        CASE i.provider
                            WHEN 'google' THEN 0
                            WHEN 'discord' THEN 1
                            ELSE 2
                        END,
                        i.updated_at DESC,
                        i.created_at DESC
                    LIMIT 1
                ) AS account_email
            FROM feedbacks f
            JOIN accounts a ON a.id = f.account_id
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return {
        "summary": {
            "total_count": int(summary_row["total_count"] if summary_row else 0),
            "account_count": int(summary_row["account_count"] if summary_row else 0),
            "recent_24h_count": int(
                (summary_row["recent_24h_count"] if summary_row else 0) or 0
            ),
            "last_created_at": summary_row["last_created_at"] if summary_row else None,
        },
        "feedbacks": [_row_to_feedback(row) for row in rows],
    }


def _row_to_feedback(row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "account_id": str(row["account_id"]),
        "account_role": row["role"],
        "account_label": row["account_label"] or "",
        "account_email": row["account_email"] or "",
        "title": row["title"],
        "content": row["content"],
        "created_at": int(row["created_at"]),
        "client_family": row["client_family"],
        "platform_name": row["platform_name"],
        "system_name": row["system_name"],
        "system_version": row["system_version"],
        "app_version": row["app_version"],
        "app_commit_hash": row["app_commit_hash"],
        "browser_name": row["browser_name"],
        "browser_version": row["browser_version"],
        "user_agent": row["user_agent"],
        "category": row["category"],
        "route_context": row["route_context"],
        "contact": row["contact"],
        "diagnostics": json.loads(row["diagnostics_json"]) if row["diagnostics_json"] else None,
    }


def _require_admin(request: Request) -> None:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
