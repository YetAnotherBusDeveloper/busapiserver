from __future__ import annotations

import asyncio
from collections.abc import Sequence
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
import threading
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette_compress import CompressMiddleware

from app.api.admin import router as admin_router
from app.api.analytics import router as analytics_router
from app.api.announcements import router as announcements_router
from app.api.account_sync import router as account_sync_router
from app.api.auth import router as auth_router
from app.api.feedback import router as feedback_router
from app.api.legal import router as legal_router
from app.api.push import router as push_router
from app.api.routes import router
from app.api.metro import router as metro_router
from app.api.rail import router as rail_router
from app.api.bike import router as bike_router
from app.api.journeys import router as journeys_router
from app.api.city_buses import router as city_buses_router
from app.city_buses import CityBusesService
from app.config import get_settings
from app.db import (
    export_download_db_if_stale,
    init_app_db,
    init_db,
    migrate_app_tables_from_main,
    refresh_database_versions,
)
from app.logging_utils import get_logger, setup_logging, shutdown_logging
from app.ntpc_opendata import NtpcOpenDataClient
from app.request_analytics import AnalyticsWriter, should_record_analytics
from app.request_budget import request_budget
from app.static_sync_control import StaticSyncCoordinator
from app.sync_realtime import RealtimeService, RouteBusesService
from app.sync_static import sync_static
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient

LOGGER = get_logger("main")
CORS_ALLOW_METHODS = ("GET", "POST", "PATCH", "PUT")
# How often the scheduler thread surfaces to check for shutdown or a queued
# manual sync. Short enough that stopping the server is not perceptibly delayed.
_SCHEDULER_TICK_SECONDS = 1.0


def _next_monday_4am(now: datetime) -> datetime:
    scheduled = now.replace(hour=4, minute=0, second=0, microsecond=0)
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday:
        scheduled = scheduled + timedelta(days=days_until_monday)
    if scheduled <= now:
        scheduled = scheduled + timedelta(days=7)
    return scheduled


def _reconcile_database_versions(settings) -> None:
    """Rebuild the download database and republish version numbers if data changed.

    Runs after startup rather than during it. The stored versions are already
    correct on a normal restart -- only sync_static changes static data, and it
    refreshes versions itself -- so this exists to catch a database replaced out
    of band while the server was down. Hashing 2.7 million rows to discover
    "nothing changed" is not worth blocking startup for.
    """
    started = time.perf_counter()
    if export_download_db_if_stale(
        settings.db_path, settings.download_db_path, settings.app_db_path
    ):
        LOGGER.info("rebuilt download db at %s", settings.download_db_path)
    refresh_database_versions(
        settings.db_path,
        download_db_path=settings.download_db_path,
        city_db_paths={
            city: settings.city_db_path(city)
            for city in settings.tdx_cities
            if settings.city_db_path(city).exists()
        },
        app_db_path=settings.app_db_path,
        trust_fingerprints=True,
    )
    LOGGER.info(
        "database versions reconciled in %.2fs", time.perf_counter() - started
    )


def _run_weekly_static_sync(app: FastAPI, stop_event: threading.Event) -> None:
    settings = app.state.settings
    coordinator: StaticSyncCoordinator = app.state.static_sync_coordinator

    try:
        _reconcile_database_versions(settings)
    except Exception as exc:
        LOGGER.exception("database version reconcile failed: %s", exc)

    while not stop_event.is_set():
        now = datetime.now()
        target = _next_monday_4am(now)
        wait_seconds = max(0.0, (target - now).total_seconds())

        # Wait in short ticks on the trigger event so that both an admin-queued
        # run and a shutdown are noticed within a second. Servicing manual runs
        # on this thread is what keeps two syncs from racing over bus.db.tmp.
        deadline = time.monotonic() + wait_seconds
        triggered = False
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if coordinator.wait_for_trigger(timeout=min(_SCHEDULER_TICK_SECONDS, remaining)):
                triggered = True
                break

        if stop_event.is_set():
            break

        if triggered:
            coordinator.run_claimed(
                lambda *, cities, force: sync_static(settings, cities=cities, force=force)
            )
            continue

        try:
            LOGGER.info("weekly static sync started")
            coordinator.begin_scheduled()
            sync_static(settings)
            coordinator.finish()
            LOGGER.info("weekly static sync completed")
        except Exception as exc:
            coordinator.finish(exc)
            LOGGER.exception("weekly static sync failed: %s", exc)
            # Avoid tight error loop.
            if not stop_event.wait(timeout=60):
                continue


async def _start_tunnel(app: FastAPI, token: str) -> None:
    try:
        from pycloudflared import Tunnel

        LOGGER.info("Starting Cloudflare tunnel...")
        tunnel = Tunnel(token=token)
        await asyncio.to_thread(tunnel.start, timeout=60)
        # Publish only once started, so a half-started tunnel is never stopped.
        app.state.tunnel = tunnel
        LOGGER.info("Cloudflare tunnel started successfully")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to start Cloudflare tunnel: %s", exc)


def _resolve_endpoint_template(request: Request) -> str | None:
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    if not isinstance(route_template, str) or not route_template:
        return None
    return route_template


def _record_request_analytics_safe(request: Request, path: str, status_code: int) -> None:
    endpoint = _resolve_endpoint_template(request)
    if not should_record_analytics(endpoint):
        return

    writer = getattr(request.app.state, "analytics_writer", None)
    if writer is None:
        return

    try:
        writer.submit(
            method=request.method,
            endpoint=endpoint,
            path=path,
            status_code=status_code,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        LOGGER.warning(
            "request analytics write failed method=%s path=%s status=%s error=%s",
            request.method,
            path,
            status_code,
            exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log_dir = setup_logging(settings.project_dir)
    init_db(settings.db_path)
    init_app_db(settings.app_db_path)
    # One-time migration for deployments that stored application data in the
    # static database. Moves auth/analytics/announcement/feedback/sync rows into
    # the dedicated application database and drops the legacy tables.
    if migrate_app_tables_from_main(settings.db_path, settings.app_db_path):
        LOGGER.info("migrated legacy application tables into app db")

    # The download db export and version refresh run in the scheduler thread
    # once startup completes -- see _reconcile_database_versions.

    token_manager = TDXTokenManager(settings)
    tdx_client = TDXClient(settings, token_manager)
    ntpc_opendata_client = NtpcOpenDataClient(request_timeout=settings.tdx_request_timeout)
    route_buses_service = RouteBusesService(settings, tdx_client)
    city_buses_service = CityBusesService(settings, tdx_client)
    realtime_service = RealtimeService(
        settings,
        tdx_client,
        route_buses_service=route_buses_service,
        ntpc_opendata_client=ntpc_opendata_client,
    )
    scheduler_stop_event = threading.Event()
    static_sync_coordinator = StaticSyncCoordinator()
    scheduler_thread = threading.Thread(
        target=_run_weekly_static_sync,
        args=(app, scheduler_stop_event),
        daemon=True,
        name="weekly-static-sync",
    )

    app.state.settings = settings
    app.state.static_sync_coordinator = static_sync_coordinator
    app.state.log_dir = log_dir
    app.state.token_manager = token_manager
    app.state.tdx_client = tdx_client
    app.state.ntpc_opendata_client = ntpc_opendata_client
    app.state.realtime_service = realtime_service
    app.state.route_buses_service = route_buses_service
    app.state.city_buses_service = city_buses_service
    app.state.scheduler_stop_event = scheduler_stop_event
    app.state.scheduler_thread = scheduler_thread
    app.state.tunnel = None
    app.state.tunnel_task = None
    analytics_writer = AnalyticsWriter(settings.app_db_path)
    app.state.analytics_writer = analytics_writer
    analytics_writer.start()

    # Nothing in the app depends on the tunnel being up, so let it connect in the
    # background instead of holding startup open for it.
    tunnel_token = os.getenv("CLOUDFLARED_TUNNEL_TOKEN")
    if tunnel_token:
        app.state.tunnel_task = asyncio.create_task(_start_tunnel(app, tunnel_token))

    scheduler_thread.start()

    try:
        yield
    finally:
        scheduler_stop_event.set()
        await asyncio.to_thread(analytics_writer.close)
        scheduler_thread.join(timeout=5)
        ntpc_opendata_client.close()
        tdx_client.close()
        token_manager.close()
        # Settle the start task first: a tunnel that finished connecting during
        # shutdown has published itself to app.state by the time this returns.
        tunnel_task = app.state.tunnel_task
        if tunnel_task is not None and not tunnel_task.done():
            tunnel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tunnel_task
        tunnel = app.state.tunnel
        if tunnel is not None:
            try:
                await asyncio.to_thread(tunnel.stop)
                LOGGER.info("Cloudflare tunnel stopped")
            except Exception as exc:
                LOGGER.exception("Failed to stop Cloudflare tunnel: %s", exc)
        shutdown_logging()


app = FastAPI(
    title="Bus API Server",
    lifespan=lifespan,
    docs_url="/info/docs",
    redoc_url="/info/redoc",
    openapi_url="/info/openapi.json",
)


def configure_cors(app: FastAPI, origins: Sequence[str]) -> None:
    if not origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        # Account sync writes use PUT, so web preflights must advertise it.
        allow_methods=list(CORS_ALLOW_METHODS),
        allow_headers=["*"],
        expose_headers=["Retry-After"],
    )


def configure_response_compression(app: FastAPI) -> None:
    app.add_middleware(
        CompressMiddleware,
        minimum_size=500,
        brotli=True,
        brotli_quality=5,
        gzip=True,
        gzip_level=5,
        zstd=False,
    )


settings = get_settings()
# get_settings() is lru_cache'd so this returns the same instance used in lifespan.
configure_cors(app, settings.cors_origins)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started_at = time.perf_counter()
    client_host = request.client.host if request.client else "unknown"
    path = request.url.path
    status_code = 500

    try:
        with request_budget(settings.realtime_request_budget_seconds):
            response = await call_next(request)
        status_code = response.status_code
    except Exception:
        duration_ms = (time.perf_counter() - started_at) * 1000
        LOGGER.exception(
            "request failed method=%s path=%s client=%s duration_ms=%.1f",
            request.method,
            path,
            client_host,
            duration_ms,
        )
        _record_request_analytics_safe(request, path, status_code)
        raise

    duration_ms = (time.perf_counter() - started_at) * 1000
    LOGGER.info(
        "request method=%s path=%s status=%s client=%s duration_ms=%.1f",
        request.method,
        path,
        status_code,
        client_host,
        duration_ms,
    )
    _record_request_analytics_safe(request, path, status_code)
    return response


configure_response_compression(app)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(analytics_router)
app.include_router(announcements_router)
app.include_router(account_sync_router)
app.include_router(feedback_router)
app.include_router(legal_router)
app.include_router(push_router)
app.include_router(router)
app.include_router(city_buses_router)
app.include_router(metro_router)
app.include_router(rail_router)
app.include_router(bike_router)
app.include_router(journeys_router)
