"""Unit tests for the keeper_mcp tool layer (PRD-21235 TC1, TC3-TC8, TC10).

The vendor SDK (`keeper_secrets_manager_core`) is mocked throughout -- no
network call ever leaves this process. Fakes model only the attributes each
tool actually reads, matched against the real SDK's classes (confirmed
2026-11-02 against keeper-secrets-manager-core==17.3.0: `Record.uid/title/
type/folder_uid/dict`, `Folder.uid/name/parent_uid`, `SecretsManager.
get_secrets/get_notation/create_secret/save/delete_secret/get_folders`,
`RecordCreate`/`RecordField`, and the exact "Folder uid=... was not
retrieved" message `create_secret` raises for a bad folder_uid).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from keeper_secrets_manager_core.exceptions import KeeperError, KeeperThrottleError

from keeper_mcp import keeper_client
from keeper_mcp.config import Settings
from keeper_mcp.middleware import _keeper_config_var
from keeper_mcp.server import create_mcp_server


@dataclass
class FakeRecord:
    uid: str
    title: str
    type: str = "login"
    folder_uid: str | None = None
    dict: dict[str, Any] = field(default_factory=lambda: {"fields": []})

    def _update(self) -> None:
        """No-op stand-in for Record._update (real SDK syncs raw_json/
        password from `.dict` here; the tool only reads `.dict` afterwards,
        so there is nothing for the fake to do)."""


@dataclass
class FakeFolder:
    uid: str
    name: str
    parent_uid: str | None = None


def _mcp():
    return create_mcp_server(Settings())


def _tool_fn(mcp, name: str):
    return mcp._tool_manager.get_tool(name).fn


def _header_configs():
    """Fixed, fake (never real) header value -- the fn under test never
    inspects it directly; only keeper_client.build_client sees it, and that
    call is monkeypatched below."""
    return "ZmFrZS1jb25maWc="  # base64("fake-config"), syntactically plausible


class FakeClient:
    def __init__(
        self,
        records: list[FakeRecord] | None = None,
        folders: list[FakeFolder] | None = None,
    ):
        self.records = records or []
        self.folders = folders or []
        self.saved: list[Any] = []
        self.created_with: tuple | None = None
        self.deleted_with: list[str] | None = None
        self.create_result: str | None = None
        self.create_error: Exception | None = None
        self.delete_response: list[dict] | None = None
        self.notation_result: Any = None

    def get_secrets(self, uids=None, full_response=False):
        if uids:
            return [r for r in self.records if r.uid in uids]
        return list(self.records)

    def get_notation(self, url):
        return self.notation_result

    def create_secret(self, folder_uid, record):
        self.created_with = (folder_uid, record)
        if self.create_error:
            raise self.create_error
        return self.create_result

    def save(self, record):
        self.saved.append(record)

    def delete_secret(self, record_uids):
        self.deleted_with = list(record_uids)
        return self.delete_response

    def get_folders(self):
        return list(self.folders)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Install a fake SecretsManager and drive it through the *real*
    middleware contextvar -> server.client_factory -> keeper_client.build_client
    wiring, exactly as a real request would (only the SDK itself is faked)."""
    client = FakeClient()

    def _build_client(config_b64):
        assert config_b64 == _header_configs()  # tool must pass the header value through
        return client

    monkeypatch.setattr(keeper_client, "build_client", _build_client)
    token = _keeper_config_var.set(_header_configs())
    yield client
    _keeper_config_var.reset(token)


# --- TC1 (AC1) ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_secrets_caps_at_limit_and_sets_has_more(fake_client: FakeClient):
    fake_client.records = [FakeRecord(uid=f"uid{i}", title=f"Secret {i}") for i in range(5)]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_list_secrets")

    result = json.loads(await fn(limit=3))

    assert len(result["secrets"]) == 3
    assert result["has_more"] is True
    assert result["secrets"][0] == {
        "uid": "uid0",
        "title": "Secret 0",
        "folder_uid": None,
        "type": "login",
    }
    # Full field values must never leak into the list view (token economy / AC1).
    assert "fields" not in result["secrets"][0]


@pytest.mark.asyncio
async def test_list_secrets_has_more_false_when_all_fit(fake_client: FakeClient):
    fake_client.records = [FakeRecord(uid="uid0", title="Only one")]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_list_secrets")

    result = json.loads(await fn(limit=50))

    assert len(result["secrets"]) == 1
    assert result["has_more"] is False


# --- TC3 (AC3) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_secret_returns_full_field_list(fake_client: FakeClient):
    fake_client.records = [
        FakeRecord(
            uid="uid1",
            title="DB creds",
            type="databaseCredentials",
            dict={"fields": [{"type": "login", "label": None, "value": ["svc"]}]},
        )
    ]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_get_secret")

    result = json.loads(await fn(uid="uid1"))

    assert result["uid"] == "uid1"
    assert result["title"] == "DB creds"
    assert result["type"] == "databaseCredentials"
    assert result["fields"] == [{"type": "login", "label": None, "value": ["svc"]}]


# --- TC4 (AC4) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_secret_unknown_uid_returns_not_found_error(fake_client: FakeClient):
    fake_client.records = []  # bound config cannot see this UID
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_get_secret")

    result = json.loads(await fn(uid="does-not-exist"))

    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False


# --- TC5 (AC5) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_secret_returns_new_uid(fake_client: FakeClient):
    fake_client.create_result = "new-uid-123"
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_create_secret")

    result = json.loads(
        await fn(
            folder_uid="folder1",
            title="New login",
            fields={"login": "svc@acme.com", "password": "s3cr3t"},
        )
    )

    assert result == {"uid": "new-uid-123"}
    called_folder_uid, record = fake_client.created_with
    assert called_folder_uid == "folder1"
    assert record.title == "New login"
    # Every field value must be normalized to a list by RecordField itself,
    # not re-wrapped here -- confirmed against keeper_secrets_manager_core's
    # own RecordField.__init__ (2026-11-02).
    field_by_type = {f.type: f.value for f in record.fields}
    assert field_by_type["login"] == ["svc@acme.com"]
    assert field_by_type["password"] == ["s3cr3t"]


@pytest.mark.asyncio
async def test_create_secret_rejects_unknown_field_type(fake_client: FakeClient):
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_create_secret")

    result = json.loads(await fn(folder_uid="folder1", title="X", fields={"not_a_real_field": "x"}))

    assert result["error"]["code"] == "invalid_argument"
    assert "not_a_real_field" in result["error"]["message"]


# --- TC6 (AC5) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_secret_invalid_folder_returns_invalid_argument(fake_client: FakeClient):
    # Exact message keeper_secrets_manager_core.SecretsManager.create_secret
    # raises for an unreachable folder_uid (confirmed against the installed
    # SDK source, 2026-11-02) -- this is what keeper_client._map_keeper_error
    # pattern-matches on.
    fake_client.create_error = KeeperError(
        "Folder uid=bad-folder was not retrieved. If you are creating a record..."
    )
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_create_secret")

    result = json.loads(await fn(folder_uid="bad-folder", title="X", fields={"login": "a"}))

    assert result["error"]["code"] == "invalid_argument"
    assert "folder_uid" in result["error"]["message"]


# --- TC7 (AC6) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_secret_removes_only_named_uids(fake_client: FakeClient):
    fake_client.delete_response = [
        {"recordUid": "uid1", "responseCode": "success"},
        {"recordUid": "uid2", "responseCode": "fail"},
    ]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_delete_secret")

    result = json.loads(await fn(uids=["uid1", "uid2"]))

    assert fake_client.deleted_with == ["uid1", "uid2"]
    assert result["deleted"] == ["uid1"]
    assert result["not_found"] == ["uid2"]


@pytest.mark.asyncio
async def test_delete_secret_rejects_empty_uid_list(fake_client: FakeClient):
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_delete_secret")

    result = json.loads(await fn(uids=[]))

    assert result["error"]["code"] == "invalid_argument"


# --- TC8 (AC6) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_secret_schema_has_no_bulk_or_folder_wide_param():
    mcp = _mcp()
    tools = await mcp.list_tools()
    delete_tool = next(t for t in tools if t.name == "keeper_delete_secret")

    properties = set(delete_tool.inputSchema.get("properties", {}).keys())

    assert properties == {"uids"}
    assert delete_tool.inputSchema.get("required") == ["uids"]


# --- tools/list snapshot (SOP A5 / §10 minimal set) -----------------------------


def test_tools_list_snapshot():
    import asyncio

    mcp = _mcp()
    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)

    assert names == [
        "keeper_create_secret",
        "keeper_delete_secret",
        "keeper_get_secret",
        "keeper_get_secret_field",
        "keeper_list_folders",
        "keeper_list_secrets",
        "keeper_update_secret",
    ]
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) <= 500, f"{tool.name} description > 500 chars"
        first_line = tool.description.splitlines()[0]
        assert len(first_line) <= 100, f"{tool.name} first description line > 100 chars"


# --- keeper_list_folders ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_folders_returns_uid_name_parent(fake_client: FakeClient):
    fake_client.folders = [FakeFolder(uid="f1", name="Root", parent_uid=None)]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_list_folders")

    result = json.loads(await fn())

    assert result == {"folders": [{"uid": "f1", "name": "Root", "parent_uid": None}]}


# --- keeper_update_secret ----------------------------------------------------


@pytest.mark.asyncio
async def test_update_secret_only_changes_named_fields(fake_client: FakeClient):
    fake_client.records = [
        FakeRecord(
            uid="uid1",
            title="X",
            dict={
                "fields": [
                    {"type": "login", "value": ["old-login"]},
                    {"type": "password", "value": ["old-pw"]},
                ]
            },
        )
    ]
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_update_secret")

    result = json.loads(await fn(uid="uid1", fields={"password": "new-pw"}))

    assert result == {"uid": "uid1", "updated_fields": ["password"]}
    assert len(fake_client.saved) == 1
    saved_record = fake_client.saved[0]
    by_type = {f["type"]: f["value"] for f in saved_record.dict["fields"]}
    assert by_type["password"] == ["new-pw"]
    assert by_type["login"] == ["old-login"]  # untouched


@pytest.mark.asyncio
async def test_update_secret_unknown_uid_returns_not_found(fake_client: FakeClient):
    fake_client.records = []
    mcp = _mcp()
    fn = _tool_fn(mcp, "keeper_update_secret")

    result = json.loads(await fn(uid="missing", fields={"password": "x"}))

    assert result["error"]["code"] == "not_found"


# --- TC10 (AC8) -- retry-then-map behaviour, exercised against the real -----
# --- keeper_client.call_with_retry / _map_keeper_error (not a tool call) ----


@pytest.mark.asyncio
async def test_upstream_rate_limit_is_retried_then_mapped_to_structured_error(
    monkeypatch: pytest.MonkeyPatch,
):
    # No real delay in tests.
    async def _no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(keeper_client.asyncio, "sleep", _no_delay)

    attempts = {"n": 0}

    def flaky_call():
        attempts["n"] += 1
        if attempts["n"] < keeper_client.MAX_RETRIES:
            import requests

            raise requests.exceptions.ConnectionError("transient network blip")
        raise KeeperThrottleError("rate limited by Keeper")

    with pytest.raises(keeper_client.ToolError) as exc_info:
        await keeper_client.call_with_retry(flaky_call, op="get_secrets")

    # The throttle error on the final attempt short-circuits straight to
    # rate_limited -- the SDK has already exhausted its own backoff before
    # raising KeeperThrottleError (see keeper_client.call_with_retry docstring).
    assert exc_info.value.code == "rate_limited"
    assert attempts["n"] == keeper_client.MAX_RETRIES


@pytest.mark.asyncio
async def test_persistent_network_error_maps_to_upstream_error_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(keeper_client.asyncio, "sleep", _no_delay)
    import requests

    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise requests.exceptions.Timeout("no response")

    with pytest.raises(keeper_client.ToolError) as exc_info:
        await keeper_client.call_with_retry(always_fails, op="get_secrets")

    assert exc_info.value.code == "upstream_error"
    assert attempts["n"] == keeper_client.MAX_RETRIES


@pytest.mark.asyncio
async def test_call_with_retry_offloads_blocking_call_so_event_loop_stays_responsive():
    """Review round 1 must-fix #2: a blocking SDK call must never stall the
    shared event loop. TC9 (test_middleware.py) only proves contextvar
    isolation under instant, mocked calls -- it does not exercise a real
    blocking call, so it would not have caught this.

    This test runs a call that blocks its own thread for ~200ms concurrently
    with a second coroutine that only advances via `asyncio.sleep` for a
    comparable ~200ms, and asserts on *wall-clock overlap*: if the blocking
    call ran directly on the event loop (the pre-fix behaviour -- confirmed
    by manually reverting to `return fn()` while writing this test, which
    makes this assertion fail at ~0.40s), the two would serialize to ~0.4s
    total. Offloading via `asyncio.to_thread` lets them run concurrently, so
    the real wall time is close to the slower of the two (~0.2s). A loose
    0.35s cutoff leaves headroom for scheduler jitter while still failing on
    serialization."""
    progress = {"ticks": 0}

    def slow_blocking_call():
        # Models a vendor SDK call that blocks the calling thread with no
        # asyncio awareness (e.g. requests.post with no timeout, or the
        # retry backoff itself if it used time.sleep instead of
        # asyncio.sleep).
        time.sleep(0.2)
        return "ok"

    async def other_task():
        for _ in range(10):
            await asyncio.sleep(0.02)
            progress["ticks"] += 1

    start = time.monotonic()
    result, _ = await asyncio.gather(
        keeper_client.call_with_retry(slow_blocking_call, op="get_secrets"),
        other_task(),
    )
    elapsed = time.monotonic() - start

    assert result == "ok"
    assert progress["ticks"] == 10
    assert elapsed < 0.35, (
        f"took {elapsed:.3f}s -- the blocking call and other_task ran "
        "serially instead of concurrently, meaning the blocking SDK call is "
        "stalling the shared event loop again"
    )
