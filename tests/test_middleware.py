"""Unit tests for GatewayCredentialsMiddleware (PRD-21235 TC2, TC9).

Pure ASGI-level tests -- no HTTP server, no network -- exercising the
middleware's contextvar isolation directly, which is the mechanism AC2, AC7
and AC9 depend on.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from keeper_mcp.config import Settings
from keeper_mcp.middleware import GatewayCredentialsMiddleware, get_keeper_config


def _scope(path: str = "/mcp", headers: dict[str, str] | None = None) -> dict:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": "http", "path": path, "method": "POST", "headers": raw_headers}


async def _empty_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _RecordingSend:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        start = next((m for m in self.messages if m["type"] == "http.response.start"), None)
        return start["status"] if start else None

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


# --- TC2 (AC2) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credential_header_returns_401_without_calling_keeper():
    inner_calls: list[str] = []

    async def inner_app(scope, receive, send):
        inner_calls.append("called")

    mw = GatewayCredentialsMiddleware(inner_app, Settings())
    send = _RecordingSend()

    await mw(_scope(headers={}), _empty_receive, send)

    assert send.status == 401
    body = json.loads(send.body)
    assert body["required_headers"] == ["X-Keeper-Config"]
    # The inner app -- and therefore any call to Keeper -- must never run.
    assert inner_calls == []


@pytest.mark.asyncio
async def test_non_mcp_path_passes_through_without_a_header():
    """/health must never require the credential header (SOP §1.1)."""
    inner_calls: list[str] = []

    async def inner_app(scope, receive, send):
        inner_calls.append("called")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = GatewayCredentialsMiddleware(inner_app, Settings())
    send = _RecordingSend()

    await mw(_scope(path="/health", headers={}), _empty_receive, send)

    assert inner_calls == ["called"]
    assert send.status == 200


@pytest.mark.asyncio
async def test_present_header_is_visible_to_inner_app_and_reset_after():
    seen: list[str | None] = []

    async def inner_app(scope, receive, send):
        seen.append(get_keeper_config())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = GatewayCredentialsMiddleware(inner_app, Settings())
    send = _RecordingSend()

    assert get_keeper_config() is None  # nothing set before the request
    await mw(_scope(headers={"X-Keeper-Config": "config-a"}), _empty_receive, send)

    assert seen == ["config-a"]
    assert get_keeper_config() is None  # reset in `finally` after the response


# --- TC9 (AC7) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_leak_credentials_or_results():
    """Two concurrent requests with two different X-Keeper-Config values must
    never observe each other's contextvar value (AC7/AC9's isolation
    guarantee -- enforced entirely by asyncio Task-local contextvars, no
    service-side tenant table to get wrong)."""
    observed: dict[str, str | None] = {}

    async def inner_app(scope, receive, send):
        tenant = scope["_test_tenant"]
        # Yield control mid-request so the two tasks genuinely interleave
        # instead of running to completion back-to-back.
        await asyncio.sleep(0)
        observed[tenant] = get_keeper_config()
        await asyncio.sleep(0)
        # Re-read after a second yield to catch any cross-task leakage that
        # only shows up after the other task has also run.
        assert get_keeper_config() == observed[tenant]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = GatewayCredentialsMiddleware(inner_app, Settings())

    async def run_request(tenant: str, config_value: str):
        scope = _scope(headers={"X-Keeper-Config": config_value})
        scope["_test_tenant"] = tenant
        await mw(scope, _empty_receive, _RecordingSend())

    await asyncio.gather(
        run_request("tenant-a", "config-a"),
        run_request("tenant-b", "config-b"),
    )

    assert observed == {"tenant-a": "config-a", "tenant-b": "config-b"}
    assert get_keeper_config() is None  # both reset, nothing leaks to the next request
