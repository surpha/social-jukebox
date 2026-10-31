"""Smoke tests: verify the app imports and core routes are wired.

These intentionally do NOT enter the TestClient context manager, so the
FastAPI lifespan (which connects to Postgres and starts workers) is not run.
That keeps the suite fast and database-free while still catching import
errors, route-wiring mistakes, and syntax regressions across the codebase.
"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_head():
    client = TestClient(app)
    resp = client.head("/health")
    assert resp.status_code == 200
