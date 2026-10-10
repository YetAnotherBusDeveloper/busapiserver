"""Standalone offline app: python -m uvicorn app.journey_demo:app."""
from pathlib import Path

from fastapi import FastAPI

from app.api.journeys import get_journey_router, router
from app.journeys import load_router

engine = load_router(Path(__file__).parent / "data" / "journey-demo-v1.json")
app = FastAPI(title="Fixed-date journey demo (synthetic data)")
app.dependency_overrides[get_journey_router] = lambda: engine
app.include_router(router)
