"""The MCP tools against a fake Polaris office API (``httpx.MockTransport``)."""

import json

import httpx
import pytest
from mcp import Client

from polaris_mcp.client import PolarisClient, Settings
from polaris_mcp.server import build_server

ME = {
    "user": {"id": 7, "name": "Afua Mensah", "role": "site_manager"},
    "company": {
        "name": "Polaris Construction",
        "currency": {"code": "XOF", "decimals": 0},
        "time_zone": "Africa/Lome",
        "today": "2025-09-02",
    },
    "token": {"name": "Claude", "can_approve": True, "expires_at": "2025-12-01T00:00:00Z"},
    "sites": [
        {"id": 3, "name": "Alpha Court", "status": "active"},
        {"id": 4, "name": "Alpha Tower", "status": "active"},
        {"id": 5, "name": "Bravo Yard", "status": "active"},
    ],
}


class FakePolaris:
    """Records requests; answers like the office API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer pol_test"
        path = request.url.path.removeprefix("/api/v1/office/")
        if path == "me/":
            return httpx.Response(200, json=ME)
        if path.startswith("sites/") and "/days/" in path:
            return httpx.Response(200, json={"day_id": 11, "status": "submitted", "path": path})
        if path == "workers/":
            return httpx.Response(200, json={"workers": [], "params": dict(request.url.params)})
        if path == "days/11/decision-preview/":
            body = json.loads(request.content)
            if body["decision"] == "reject" and not body["note"]:
                return httpx.Response(
                    400,
                    json={
                        "code": "validation_error",
                        "message": "Invalid input.",
                        "details": {"note": ["Tell the foreman what to check."]},
                    },
                )
            return httpx.Response(200, json={"decision": body["decision"], "confirmation": "c1"})
        if path == "decisions/":
            return httpx.Response(
                409,
                json={
                    "code": "conflict",
                    "message": "The roll call changed after the preview. Preview it again.",
                    "details": {"status": "submitted"},
                },
            )
        return httpx.Response(404, json={"code": "not_found", "message": "Not found."})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake():
    return FakePolaris()


@pytest.fixture
def server(fake):
    settings = Settings(url="https://polaris.example", token="pol_test")
    return build_server(PolarisClient(settings, transport=httpx.MockTransport(fake)))


def data(result):
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_the_tools_and_their_hints(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {
        "whoami",
        "today_overview",
        "pending_approvals",
        "site_roll_call",
        "cash_sheet",
        "find_workers",
        "worker_history",
        "preview_roll_call_decision",
        "confirm_roll_call_decision",
    }
    writes = {name for name, tool in tools.items() if not tool.annotations.read_only_hint}
    assert writes == {"confirm_roll_call_decision"}
    assert tools["confirm_roll_call_decision"].annotations.destructive_hint is True
    # Nothing that sounds like paying.
    assert not any("pay" in name for name in tools if name != "cash_sheet")


@pytest.mark.anyio
async def test_site_names_and_relative_dates_are_resolved(server, fake):
    async with Client(server) as client:
        day = data(await client.call_tool("site_roll_call", {"site": "bravo", "date": "yesterday"}))
        ambiguous = await client.call_tool("site_roll_call", {"site": "alpha", "date": "today"})
        unknown = await client.call_tool("site_roll_call", {"site": "Zulu", "date": "today"})
        by_id = data(await client.call_tool("site_roll_call", {"site": "4", "date": "2025-09-01"}))

    assert day["path"] == "sites/5/days/2025-09-01/"
    assert ambiguous.is_error and "Alpha Court, Alpha Tower" in ambiguous.content[0].text
    assert unknown.is_error and "Your sites" in unknown.content[0].text
    assert by_id["path"] == "sites/4/days/2025-09-01/"


@pytest.mark.anyio
async def test_find_workers_passes_the_filters(server):
    async with Client(server) as client:
        found = data(await client.call_tool("find_workers", {"query": "yao", "site": "Court"}))
    assert found["params"] == {"q": "yao", "site": "3"}


@pytest.mark.anyio
async def test_preview_then_confirm_and_api_errors_reach_the_model(server, fake):
    async with Client(server) as client:
        preview = data(
            await client.call_tool(
                "preview_roll_call_decision", {"day_id": 11, "decision": "approve"}
            )
        )
        missing_note = await client.call_tool(
            "preview_roll_call_decision", {"day_id": 11, "decision": "reject"}
        )
        conflict = await client.call_tool("confirm_roll_call_decision", {"confirmation": "c1"})

    assert preview["confirmation"] == "c1" and "ask them to confirm" in preview["next_step"]
    assert missing_note.is_error
    assert "Tell the foreman what to check" in missing_note.content[0].text
    assert conflict.is_error and "Preview it again" in conflict.content[0].text
    assert [r.method for r in fake.requests if r.url.path.endswith("/decisions/")] == ["POST"]


def test_settings_refuse_plain_http_and_non_agent_tokens(monkeypatch):
    monkeypatch.setenv("POLARIS_URL", "http://polaris.example")
    monkeypatch.setenv("POLARIS_TOKEN", "pol_x")
    with pytest.raises(SystemExit, match="https"):
        Settings.from_env()

    monkeypatch.setenv("POLARIS_URL", "http://localhost:8000/")
    assert Settings.from_env().url == "http://localhost:8000"

    monkeypatch.setenv("POLARIS_TOKEN", "Device abc")
    with pytest.raises(SystemExit, match="agent token"):
        Settings.from_env()
