from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.rate_limit import enforce_rate_limit, get_request_principal
from app.request_analytics import build_analytics_report


router = APIRouter(tags=["analytics"], dependencies=[Depends(enforce_rate_limit)])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


@router.get("/api/v1/admin/analytics")
def get_analytics(
    request: Request,
    days: int = Query(default=7, ge=0, le=365),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    _require_admin(request)
    settings = request.app.state.settings
    return build_analytics_report(settings.app_db_path, days=days, limit=limit)


@router.get("/api/v1/admin/eta-observations")
def get_eta_observation_status(request: Request) -> dict[str, object]:
    """Health of the passive ETA observation store (counters only, no data)."""
    _require_admin(request)
    collector = getattr(request.app.state, "eta_observations", None)
    if collector is None:
        return {"enabled": False}
    return collector.status()


@router.get("/api/v1/analytics", include_in_schema=False)
def legacy_get_analytics(
    request: Request,
    days: int = Query(default=7, ge=0, le=365),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_analytics(request, days=days, limit=limit)


@router.get("/analytics", include_in_schema=False)
def legacy_analytics_dashboard() -> RedirectResponse:
    return RedirectResponse("/admin/analytics", status_code=302)


@router.get(
    "/admin/analytics",
    include_in_schema=False,
    response_model=None,
)
def analytics_dashboard(request: Request) -> FileResponse | RedirectResponse:
    if get_request_principal(request) is None:
        return RedirectResponse("/auth", status_code=302)
    _require_admin(request)
    return FileResponse(WEB_ROOT / "analytics.html", media_type="text/html")


def _require_admin(request: Request) -> None:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
