from functools import lru_cache
import os
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException

from app.journeys import JourneyQuery, JourneyResult, Router, load_router

router = APIRouter(prefix="/api/v1/journeys", tags=["journeys"])
_load_lock = Lock()


@lru_cache(maxsize=1)
def _load_configured_router() -> Router:
    path = os.getenv("JOURNEY_TIMETABLE_PATH")
    if not path:
        raise HTTPException(503, "Set JOURNEY_TIMETABLE_PATH to an absolute fixed-date timetable JSON path")
    try:
        configured_path = Path(path)
        if not configured_path.is_absolute():
            raise ValueError("JOURNEY_TIMETABLE_PATH must be absolute")
        return load_router(configured_path)
    except (OSError, ValueError):
        raise HTTPException(503, "Journey timetable is unavailable or invalid") from None


def get_journey_router() -> Router:
    # lru_cache alone can invoke its function concurrently on cold misses.
    # Lock OUTSIDE the cached call so waiters reuse the first successful load.
    # Exceptions are not cached: fixing a bad file allows a subsequent retry.
    with _load_lock:
        return _load_configured_router()


@router.post("", response_model=JourneyResult)
def plan_journey(query: JourneyQuery, engine: Router = Depends(get_journey_router)) -> JourneyResult:
    try:
        return engine.route(query)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
