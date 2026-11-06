from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.auth_service import (
    AuthError,
    AuthFlowResult,
    AuthLoginResult,
    PendingAccountMergePreview,
    account_devices_payload,
    account_payload,
    build_authorization_url,
    build_error_redirect_url,
    build_link_redirect_url,
    build_login_redirect_url,
    complete_google_native_login,
    complete_oauth_flow,
    confirm_account_merge,
    create_oauth_state,
    create_link_oauth_state_context,
    consume_oauth_state,
    default_redirect_uri,
    get_device_key,
    get_pending_account_merge,
    normalize_redirect_uri,
    revoke_all_tokens,
    revoke_token,
    validate_device_key,
    validate_platform,
    validate_provider,
)
from app.rate_limit import enforce_rate_limit, get_request_principal


router = APIRouter(tags=["auth"], dependencies=[Depends(enforce_rate_limit)])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class GoogleNativeLoginRequest(BaseModel):
    id_token: str = Field(min_length=1)
    device_key: str = Field(min_length=1)
    device_name: str = Field(default="")


class OAuthLinkStartRequest(BaseModel):
    platform: str = Field(default="web", min_length=1)
    redirect: str = Field(default="")


class AccountMergeConfirmRequest(BaseModel):
    merge_token: str = Field(min_length=1)


@router.get("/auth", include_in_schema=False)
def auth_page() -> FileResponse:
    return _page_response("auth.html")


@router.get("/auth/complete", include_in_schema=False)
def auth_complete_page() -> FileResponse:
    return _page_response("auth_complete.html")


@router.get("/account", include_in_schema=False, response_model=None)
def account_page(request: Request) -> FileResponse | RedirectResponse:
    if get_request_principal(request) is None:
        return RedirectResponse("/auth", status_code=302)
    return _page_response("account.html")


@router.get("/api/v1/auth/discord-start")
def start_discord_login(
    request: Request,
    platform: str = Query(default="web"),
    redirect: str = Query(default=""),
    device_key: str = Query(...),
) -> RedirectResponse:
    return _start_oauth_login(
        request,
        provider="discord",
        platform=platform,
        redirect=redirect,
        device_key=device_key,
    )


@router.get("/api/v1/auth/google-start")
def start_google_login(
    request: Request,
    platform: str = Query(default="web"),
    redirect: str = Query(default=""),
    device_key: str = Query(...),
) -> RedirectResponse:
    return _start_oauth_login(
        request,
        provider="google",
        platform=platform,
        redirect=redirect,
        device_key=device_key,
    )


@router.get("/api/v1/auth/discord-callback")
def discord_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    return _oauth_callback(
        request, provider="discord", code=code, state=state, error=error,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/api/v1/auth/google-callback")
def google_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    return _oauth_callback(
        request, provider="google", code=code, state=state, error=error,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/api/v1/auth/google-native")
def google_native_login(request: Request, payload: GoogleNativeLoginRequest) -> dict[str, str]:
    try:
        result = complete_google_native_login(
            request.app.state.settings,
            id_token=payload.id_token,
            device_key=payload.device_key,
            device_name=payload.device_name.strip() or None,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _login_result_payload(result)


@router.get("/api/v1/auth/me")
def auth_me(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return account_payload(request.app.state.settings, principal)


@router.get("/api/v1/auth/devices")
def auth_devices(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return account_devices_payload(request.app.state.settings, principal)


@router.post("/api/v1/auth/link/discord-start")
def start_discord_link(request: Request, payload: OAuthLinkStartRequest) -> dict[str, str]:
    return _start_oauth_link(
        request,
        provider="discord",
        platform=payload.platform,
        redirect=payload.redirect,
    )


@router.post("/api/v1/auth/link/google-start")
def start_google_link(request: Request, payload: OAuthLinkStartRequest) -> dict[str, str]:
    return _start_oauth_link(
        request,
        provider="google",
        platform=payload.platform,
        redirect=payload.redirect,
    )


@router.get("/api/v1/auth/link/pending")
def auth_link_pending(
    request: Request,
    merge_token: str = Query(..., min_length=1),
) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        preview = get_pending_account_merge(
            request.app.state.settings,
            target_account_id=principal.account_id,
            merge_token=merge_token,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _merge_preview_payload(preview)


@router.post("/api/v1/auth/link/confirm")
def auth_link_confirm(
    request: Request,
    payload: AccountMergeConfirmRequest,
) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        result = confirm_account_merge(
            request.app.state.settings,
            target_account_id=principal.account_id,
            merge_token=payload.merge_token,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "ok": True,
        "status": result.status,
        "provider": result.provider,
    }


@router.post("/api/v1/auth/logout")
def auth_logout(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    revoke_token(request.app.state.settings, principal)
    return {"ok": True}


@router.post("/api/v1/auth/logout-all")
def auth_logout_all(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    revoke_all_tokens(request.app.state.settings, principal)
    return {"ok": True}


def _start_oauth_login(
    request: Request,
    *,
    provider: str,
    platform: str,
    redirect: str,
    device_key: str,
) -> RedirectResponse:
    try:
        auth_provider = validate_provider(provider)
        auth_platform, redirect_uri = _resolve_redirect_uri(
            request,
            platform=platform,
            redirect=redirect,
            default_web_path="/auth/complete",
        )
        normalized_device_key = validate_device_key(device_key)
        settings = request.app.state.settings
        state = create_oauth_state(
            settings,
            provider=auth_provider,
            platform=auth_platform,
            redirect_uri=redirect_uri,
            device_key=normalized_device_key,
        )
        authorization_url = build_authorization_url(
            settings,
            provider=auth_provider,
            state=state,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return RedirectResponse(authorization_url, status_code=302)


def _start_oauth_link(
    request: Request,
    *,
    provider: str,
    platform: str,
    redirect: str,
) -> dict[str, str]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        auth_provider = validate_provider(provider)
        auth_platform, redirect_uri = _resolve_redirect_uri(
            request,
            platform=platform,
            redirect=redirect,
            default_web_path="/account",
        )
        settings = request.app.state.settings
        state = create_oauth_state(
            settings,
            provider=auth_provider,
            platform=auth_platform,
            redirect_uri=redirect_uri,
            device_key=get_device_key(settings, principal.device_id),
        )
        create_link_oauth_state_context(
            settings,
            state=state,
            target_account_id=principal.account_id,
        )
        authorization_url = build_authorization_url(
            settings,
            provider=auth_provider,
            state=state,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return {"authorization_url": authorization_url}


def _oauth_callback(
    request: Request,
    *,
    provider: str,
    code: str,
    state: str,
    error: str,
    user_agent: str | None = None,
) -> RedirectResponse:
    auth_provider = validate_provider(provider)
    fallback_redirect = default_redirect_uri("web")
    settings = request.app.state.settings
    if error:
        redirect_uri = fallback_redirect
        if state:
            try:
                redirect_uri = consume_oauth_state(
                    settings,
                    provider=auth_provider,
                    state=state,
                ).redirect_uri
            except AuthError:
                redirect_uri = fallback_redirect
        return RedirectResponse(
            build_error_redirect_url(redirect_uri, error),
            status_code=302,
        )
    if not code or not state:
        return RedirectResponse(
            build_error_redirect_url(fallback_redirect, "missing_code_or_state"),
            status_code=302,
        )

    try:
        result = complete_oauth_flow(
            settings,
            provider=auth_provider,
            code=code,
            state=state,
            user_agent=user_agent,
        )
    except AuthError as exc:
        return RedirectResponse(
            build_error_redirect_url(exc.redirect_uri or fallback_redirect, exc.message),
            status_code=302,
        )

    mcp_provider = getattr(request.app.state, "mcp_provider", None)
    if mcp_provider is not None:
        from app.mcp_auth import finish_login

        mcp_response = finish_login(mcp_provider, request, result)
        if mcp_response is not None:
            return mcp_response
    return _oauth_success_redirect(result)


def _login_result_payload(result: AuthLoginResult) -> dict[str, str]:
    return {
        "token": result.token,
        "account_id": str(result.account_id),
        "device_id": str(result.device_id),
        "role": result.role,
        "provider": result.provider,
        "display_name": result.display_name or "",
    }


def _page_response(name: str) -> FileResponse:
    return FileResponse(WEB_ROOT / name, media_type="text/html")


def _resolve_redirect_uri(
    request: Request,
    *,
    platform: str,
    redirect: str,
    default_web_path: str,
) -> tuple[str, str]:
    auth_platform = validate_platform(platform)
    if auth_platform == "web" and not redirect.strip():
        settings = request.app.state.settings
        return auth_platform, f"{settings.auth_public_base_url}{default_web_path}"
    return auth_platform, normalize_redirect_uri(redirect, auth_platform)


def _oauth_success_redirect(result: AuthFlowResult) -> RedirectResponse:
    if result.login_result is not None:
        redirect_url = build_login_redirect_url(result.redirect_uri, result.login_result)
    elif result.link_result is not None:
        redirect_url = build_link_redirect_url(result.redirect_uri, result.link_result)
    else:
        redirect_url = build_error_redirect_url(result.redirect_uri, "invalid_oauth_result")
    return RedirectResponse(redirect_url, status_code=302)


def _merge_preview_payload(preview: PendingAccountMergePreview) -> dict[str, object]:
    return {
    "provider": preview.provider,
    "target_account_id": str(preview.target_account_id),
    "source_account_id": str(preview.source_account_id),
    "expires_at": preview.expires_at,
    "active_device_count": preview.active_device_count,
    "source_identities": [
        {
        "provider": identity.provider,
        "email": identity.email,
        "display_name": identity.display_name,
        }
        for identity in preview.source_identities
    ],
    }
