# -*- coding: utf-8 -*-

"""Tests for the SSE /dashboard/api/events endpoint."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from unittest.mock import MagicMock

from kiro.routes_dashboard import router
from kiro.config import PROXY_API_KEY


def _make_app():
    app = FastAPI()
    app.include_router(router)
    app.state.account_manager = MagicMock()
    app.state.account_manager.get_accounts_snapshot.return_value = []
    return app


def test_events_endpoint_requires_auth():
    from fastapi.testclient import TestClient
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/dashboard/api/events")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_events_endpoint_streams_snapshot_first():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {PROXY_API_KEY}"}
        async with client.stream("GET", "/dashboard/api/events", headers=headers) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            collected = ""
            async for chunk in resp.aiter_text():
                collected += chunk
                if "event: snapshot" in collected:
                    break
            assert "event: snapshot" in collected
