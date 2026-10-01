"""Tests for /v1/runs endpoints: start, status, events, steer, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/steer — inject guidance into a running agent
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _api_request_profile,
    _approval_event_choices,
    _start_api_run_budget_watchdog,
    cors_middleware,
    security_headers_middleware,
    _publish_turn_process_ownership,
)
from tools import approval as approval_mod
from tools import approval_gateway_wait


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart_denied", "allow_session", "allow_permanent", "expected"),
    [
        (False, True, True, ["once", "session", "always", "deny"]),
        (False, True, False, ["once", "session", "deny"]),
        (False, False, True, ["once", "deny"]),
        (False, False, False, ["once", "deny"]),
        (True, True, True, ["once", "deny"]),
        (True, False, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_session, allow_permanent, expected
):
    assert (
        _approval_event_choices(
            smart_denied=smart_denied,
            allow_session=allow_session,
            allow_permanent=allow_permanent,
        )
        == expected
    )


def test_api_run_budget_watchdog_requests_hard_interrupt(monkeypatch):
    observed = {}

    class FakeTimer:
        def __init__(self, interval, callback):
            observed["interval"] = interval
            observed["callback"] = callback
            self.daemon = False

        def start(self):
            observed["started"] = True

        def cancel(self):
            observed["cancelled"] = True

    agent = MagicMock()
    agent.run_budget_seconds = 1200
    interrupt = MagicMock()
    monkeypatch.setattr(
        "gateway.platforms.api_server.threading.Timer", FakeTimer
    )
    monkeypatch.setattr(
        "gateway.platforms.api_server.request_hard_interrupt", interrupt
    )

    watchdog = _start_api_run_budget_watchdog(agent, "run_bounded")
    assert watchdog is not None
    assert observed["interval"] == 1200
    assert observed["started"] is True
    assert watchdog.daemon is True

    observed["callback"]()
    interrupt.assert_called_once_with(
        agent, "API run wall-clock budget exhausted after 1200s"
    )


@pytest.mark.parametrize("budget", [None, 0, -1, "invalid"])
def test_api_run_budget_watchdog_is_dormant_without_positive_budget(budget):
    agent = MagicMock()
    agent.run_budget_seconds = budget
    assert _start_api_run_budget_watchdog(agent, "run_unbounded") is None


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    adapter = APIServerAdapter(config)
    return adapter


def _claim_run(adapter: APIServerAdapter, run_id: str) -> None:
    """Stamp *run_id* as owned by the unprefixed (default) request scope."""
    request = MagicMock()
    request.headers = {}
    adapter._run_owners[run_id] = adapter._run_idempotency_scope(request)


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    """Create an aiohttp app with /v1/runs routes registered."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/workflow-profile/readiness", adapter._handle_workflow_readiness)
    app.router.add_get("/v1/workflow-attempts/{attempt_id}", adapter._handle_workflow_attempt)
    app.router.add_post("/v1/workflow-attempts/{attempt_id}/abort", adapter._handle_abort_workflow_attempt)
    app.router.add_post(
        "/v1/room-members/invitations",
        adapter._handle_room_member_invitation,
    )
    app.router.add_get(
        "/v1/room-members/capabilities",
        adapter._handle_room_member_capabilities,
    )
    app.router.add_post(
        "/v1/room-members/grants/refresh",
        adapter._handle_room_member_grant_refresh,
    )
    app.router.add_post(
        "/v1/room-members/grants/revoke",
        adapter._handle_room_member_grant_revoke,
    )
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _make_slow_agent(**kwargs):
    """Create a mock agent that blocks in run_conversation until interrupted.

    Returns (mock_agent, agent_ready_event, interrupt_event) where
    agent_ready_event is set once run_conversation starts, and
    interrupt_event is set when interrupt() is called.
    """
    ready = threading.Event()
    interrupted = threading.Event()

    mock_agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    mock_agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _slow_run(user_message=None, conversation_history=None, task_id=None):
        ready.set()
        # Block until interrupt() is called
        interrupted.wait(timeout=10)
        return {"final_response": "interrupted", "interrupted": True}

    mock_agent.run_conversation.side_effect = _slow_run
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0

    return mock_agent, ready, interrupted


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# POST /v1/runs — start a run
# ---------------------------------------------------------------------------


class TestStartRun:
    @pytest.mark.asyncio
    async def test_room_auth_is_validated_before_body_parse_or_work_reservation(
        self, auth_adapter
    ):
        from gateway.platforms import api_server_runs

        app = _create_runs_app(auth_adapter)
        handler = AsyncMock()
        with patch.object(api_server_runs, "_handle_runs", handler):
            async with TestClient(TestServer(app)) as cli:
                response = await cli.post(
                    "/v1/runs",
                    data="{this body must never be parsed",
                    headers={
                        "Authorization": "HermesRoom invalid-token",
                        "Content-Type": "application/json",
                    },
                )
                body = await response.json()

        assert response.status == 401
        assert body["error"]["code"] == "invalid_room_grant"
        assert auth_adapter._pending_agent_requests == 0
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_returns_202(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "started"
                assert data["run_id"].startswith("run_")

                status_resp = await cli.get(f"/v1/runs/{data['run_id']}")
                assert status_resp.status == 200
                status = await status_resp.json()
                assert status["run_id"] == data["run_id"]
                assert status["status"] in {"queued", "running", "completed"}
                assert status["object"] == "hermes.run"

    def test_sweep_repairs_nonterminal_status_without_live_task(self, adapter):
        """A lost final status update must not poll as running forever."""
        now = time.time()
        adapter._run_statuses = {
            "run-orphan": {
                "object": "hermes.run",
                "run_id": "run-orphan",
                "status": "running",
                "created_at": now - 30,
                "updated_at": now - 20,
            }
        }
        adapter._active_run_tasks = {}
        stream = MagicMock()
        adapter._run_streams = {"run-orphan": stream}

        adapter._sweep_orphaned_runs_once(now)

        status = adapter._run_statuses["run-orphan"]
        assert status["status"] == "failed"
        assert status["last_event"] == "run.failed"
        assert status["error"] == (
            "Run execution ended without a terminal status update"
        )
        assert adapter.active_agent_work_count() == 0
        terminal_event = stream.put_nowait.call_args_list[0].args[0]
        assert terminal_event["event"] == "run.failed"
        assert terminal_event["run_id"] == "run-orphan"
        assert stream.put_nowait.call_args_list[1].args[0] is None

    @pytest.mark.asyncio
    async def test_structured_provider_failure_releases_live_work(self, adapter):
        """A terminal provider error must release the real API slot."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {
                    "final_response": "API call failed after 2 retries",
                    "failed": True,
                    "error": "connection closed",
                    "failure_reason": "timeout",
                }
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                response = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await response.json())["run_id"]
                for _ in range(40):
                    status = adapter._run_statuses[run_id]
                    if status["status"] == "failed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "failed"
                assert adapter.active_agent_work_count() == 0
                assert run_id not in adapter._active_run_tasks
                assert run_id not in adapter._active_run_agents
                mock_agent.close.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_opt_in_reaps_run_owned_background_processes(self, adapter, monkeypatch):
        """A task-runner policy cleans up session-owned local previews."""
        from tools.process_registry import process_registry

        adapter._reap_background_processes_on_run_completion = True
        snapshots = []
        reaps = []
        monkeypatch.setattr(
            process_registry,
            "snapshot_running_ids",
            lambda task_id, *, session_key="": snapshots.append(
                (task_id, session_key)
            )
            or frozenset({"proc-before"}),
        )
        monkeypatch.setattr(
            process_registry,
            "kill_started_since",
            lambda task_id, baseline, *, source, session_key="": reaps.append(
                (task_id, baseline, source, session_key)
            )
            or 1,
        )

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {
                    "final_response": "done"
                }
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                for _ in range(40):
                    if reaps:
                        break
                    await asyncio.sleep(0.05)

        assert snapshots == [(run_id, run_id)]
        assert reaps == [
            (
                run_id,
                frozenset({"proc-before"}),
                "api_server_run_completion",
                run_id,
            )
        ]

    @pytest.mark.asyncio
    async def test_start_binds_chat_id_for_delegation_wake_target(self, adapter):
        """/v1/runs must bind the raw session id as the api_server chat_id
        (like every other agent-entry route does via _run_agent): the async
        delegation dispatch reads HERMES_SESSION_CHAT_ID to pick its wake
        self-post target, and an empty binding forces background delegations
        on this route back to synchronous execution."""
        app = _create_runs_app(adapter)
        captured = {}

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()

                def _capture_run(user_message=None, conversation_history=None, task_id=None):
                    from tools.async_delegation import _current_origin_session_id

                    captured["origin_session_id"] = _current_origin_session_id()
                    return {"final_response": "done"}

                mock_agent.run_conversation.side_effect = _capture_run
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "runs-raw-sid"},
                )
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert captured.get("origin_session_id") == "runs-raw-sid", (
            "runs route must bind chat_id so delegation dispatch sees a wake target"
        )

    @staticmethod
    async def _wait_completed(cli, run_id: str) -> None:
        for _ in range(40):
            status = await (await cli.get(f"/v1/runs/{run_id}")).json()
            if status["status"] == "completed":
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} did not complete")

    @staticmethod
    def _capturing_agent(captured):
        agent = MagicMock()
        agent.run_conversation.side_effect = lambda **kwargs: captured.update(kwargs) or {"final_response": "done"}
        agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
        return agent

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body, expected", [
        ({"input": "hello", "author": {"id": "bot:dixie", "name": " dixie ", "is_bot": True, "role": "admin"}},
         {"id": "bot:dixie", "name": "dixie", "is_bot": True}),
        ({"input": "hello", "author": {"id": "bot:dixie", "name": "dixie", "is_bot": True, "origin": "cloud-1"}},
         {"id": "bot:cloud-1/dixie", "name": "dixie", "is_bot": True}),
        ({"input": "hello"}, "absent"),
    ], ids=["author", "author with origin", "no author"])
    async def test_start_passes_normalized_author_to_run_conversation(self, adapter, body, expected):
        """A body ``author`` reaches ``run_conversation`` normalized; it labels memory only. Without one the
        call keeps today's shape."""
        app = _create_runs_app(adapter)
        captured = {}

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", return_value=self._capturing_agent(captured)):
                resp = await cli.post("/v1/runs", json=body)
                assert resp.status == 202
                await self._wait_completed(cli, (await resp.json())["run_id"])

        assert captured["user_message"] == "hello"
        assert captured.get("turn_author", "absent") == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("author", ["dixie", ["dixie"], 7])
    async def test_start_rejects_non_object_author(self, adapter, author):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                resp = await cli.post("/v1/runs", json={"input": "hello", "author": author})
                assert resp.status == 400
                body = await resp.json()
        assert body["error"]["code"] == "invalid_author"
        mock_create.assert_not_called()
        assert adapter._run_statuses == {}


    @pytest.mark.asyncio
    async def test_start_rejects_conflicting_route_and_request_provider(self):
        adapter = APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "model_routes": {
                        "alias": {
                            "model": "route/model",
                            "provider": "openrouter",
                        }
                    }
                },
            )
        )
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "alias",
                        "provider": "minimax",
                    },
                )
                data = await resp.json()

        assert resp.status == 400
        assert "provider" in data["error"]["message"].lower()
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_stream_forwards_interim_commentary(self, adapter):
        """Mid-turn assistant commentary (Codex ``phase="commentary"``) reaches /v1/runs clients
        as ``message.interim`` {text, already_streamed}; the final answer is unchanged (#67580)."""
        import json

        app = _create_runs_app(adapter)

        def create_agent(**kwargs):
            interim = kwargs["interim_assistant_callback"]
            agent = MagicMock()

            def run_conversation(**_kw):
                interim("Checking the docs first.", already_streamed=False)
                interim("Applying the fix.", already_streamed=True)
                return {"final_response": "Done."}

            agent.run_conversation.side_effect = run_conversation
            agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
            return agent

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", side_effect=create_agent):
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                body = await (await cli.get(f"/v1/runs/{run_id}/events")).text()

        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        interim = [(e["text"], e["already_streamed"]) for e in events if e["event"] == "message.interim"]
        assert interim == [("Checking the docs first.", False), ("Applying the fix.", True)]
        completed = next(e for e in events if e["event"] == "run.completed")
        assert completed["output"] == "Done."

    @pytest.mark.asyncio
    async def test_start_passes_request_model_provider_options_to_create_agent(self, adapter):
        app = _create_runs_app(adapter)
        model_options = {"reasoning_effort": "medium", "service_tier": "priority"}
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "MiniMax-M3",
                        "provider": "minimax",
                        "model_options": model_options,
                    },
                )
                assert resp.status == 202
                for _ in range(20):
                    if mock_create.call_args is not None:
                        break
                    await asyncio.sleep(0.05)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["requested_model"] == "MiniMax-M3"
        assert kwargs["requested_provider"] == "minimax"
        assert kwargs["model_options"] == model_options


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id} — poll run status
# ---------------------------------------------------------------------------


class TestRunStatus:

    @pytest.mark.asyncio
    async def test_drain_boundary_is_visible_to_pollers_on_live_runs_only(self, adapter):
        """GET /v1/runs/{id} shows ``shutdown_requested_at`` as soon as the drain starts (#115133).

        A live run keeps ``status: running`` (it is still being served) but gains the marker,
        durably (the idempotency record carries it across a restart); a run whose status is set
        after the boundary inherits it; a terminal run is never touched.
        """
        status = adapter._set_run_status("run_live", "running")
        _claim_run(adapter, "run_live")
        scope = adapter._run_owners["run_live"]
        adapter._run_idempotency_store.reserve(
            scope, "shutdown-test-key", "shutdown-test-fingerprint", "run_live", status)
        adapter._run_idempotency_ids.add("run_live")
        adapter._run_statuses["run_done"] = {
            "object": "hermes.run", "run_id": "run_done", "status": "completed"}
        _claim_run(adapter, "run_done")

        async with TestClient(TestServer(_create_runs_app(adapter))) as client:
            before = await (await client.get("/v1/runs/run_live")).json()
            assert "shutdown_requested_at" not in before

            assert adapter.mark_shutdown_requested() == 1

            live = await (await client.get("/v1/runs/run_live")).json()
            assert live["status"] == "running"
            marker = live["shutdown_requested_at"]
            assert isinstance(marker, float)
            done = await (await client.get("/v1/runs/run_done")).json()
            assert "shutdown_requested_at" not in done

        durable = adapter._run_idempotency_store.status_for_run(scope, "run_live")
        assert durable["status"].get("shutdown_requested_at") == marker
        adapter._set_run_status("run_late", "queued")
        assert adapter._run_statuses["run_late"]["shutdown_requested_at"] == marker

    @pytest.mark.asyncio
    async def test_status_reflects_explicit_session_id(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "space-session"},
                )
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_agent.run_conversation.assert_called_once()
                assert mock_agent.run_conversation.call_args.kwargs["task_id"] == "space-session"
                assert status["session_id"] == "space-session"

    @pytest.mark.asyncio
    async def test_status_completed_run_reports_served_runtime_and_cache_tokens(self, adapter):
        """After a fallback_providers switch the run record carries the runtime that actually
        served the turn plus cache-read tokens, next to the requested ``model`` (#102101).

        ``agent.provider`` / ``agent.model`` still hold the fallback pair when
        ``run_conversation()`` returns: the primary is only restored at the start of the NEXT
        turn, so they are the served pair, while the top-level ``model`` echoes the request.
        """
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.provider = "openai-codex"
                mock_agent.model = "gpt-5.6-luna"
                mock_agent.session_prompt_tokens = 100
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 105
                mock_agent.session_cache_read_tokens = 84
                mock_agent.session_cache_write_tokens = 11
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello", "model": "deepseek-v4-pro"})
                run_id = (await resp.json())["run_id"]

                for _ in range(40):
                    status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "completed"
                # Top-level model still echoes the request; the served pair is disclosed alongside.
                assert status["model"] == "deepseek-v4-pro"
                # Canonical api_server runtime shape (same as /v1/chat/completions), not a thinner twin.
                assert status["runtime"] == {
                    "provider": "openai-codex", "model": "gpt-5.6-luna", "route_source": "raw_request",
                    "requested": {"provider": "", "model": "deepseek-v4-pro"},
                }
                assert status["usage"] == {
                    "input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
                    "cache_read_tokens": 84, "cache_write_tokens": 11,
                }


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events — SSE event stream
# ---------------------------------------------------------------------------


class TestRunEvents:
    @pytest.mark.asyncio
    async def test_tool_completed_event_includes_redacted_bounded_result_preview(self, adapter):
        loop = asyncio.get_running_loop()
        adapter._run_streams["run_tool"] = asyncio.Queue()
        callback = adapter._make_run_event_callback("run_tool", loop)

        callback(
            "tool.completed", "terminal", duration=0.077, is_error=True,
            result={
                "exit_code": 2,
                "error": "BLOCKED: approval required",
                "token": "sk-abcdefghijklmnopqrstuvwxyz",
                "output": "x" * 600,
            },
        )
        event = await adapter._run_streams["run_tool"].get()

        assert event["error"] is True
        assert "BLOCKED: approval required" in event["preview"]
        assert "abcdefghijklmnopqrstuvwxyz" not in event["preview"]
        assert len(event["preview"]) <= 500
        assert event["preview"].endswith("...")

    @pytest.mark.asyncio
    async def test_events_stream_returns_completed(self, adapter):
        """Events stream should receive run.completed when agent finishes."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "Hello!"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Subscribe to events
                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                # Should contain run.completed
                assert "run.completed" in body
                assert "Hello!" in body

    @pytest.mark.asyncio
    async def test_completed_event_carries_served_runtime_and_cache_tokens(self, adapter):
        """The run.completed SSE event discloses the same served runtime and cache tokens as the
        pollable status, so streaming clients get identical cost-attribution data (#102101)."""
        import json as _json

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "served"}
                mock_agent.provider = "openai-codex"
                mock_agent.model = "gpt-5.6-luna"
                mock_agent.session_prompt_tokens = 774050
                mock_agent.session_completion_tokens = 6286
                mock_agent.session_total_tokens = 780336
                mock_agent.session_cache_read_tokens = 650000
                mock_agent.session_cache_write_tokens = 42
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello", "model": "deepseek-v4-pro"})
                run_id = (await resp.json())["run_id"]

                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                completed = None
                for frame in body.split("\n"):
                    if frame.startswith("data: "):
                        try:
                            payload = _json.loads(frame[len("data: "):])
                        except ValueError:
                            continue
                        if payload.get("event") == "run.completed":
                            completed = payload
                            break
                assert completed is not None, "run.completed event missing from stream"
                assert completed["runtime"]["provider"] == "openai-codex"
                assert completed["runtime"]["model"] == "gpt-5.6-luna"
                assert completed["runtime"]["requested"]["model"] == "deepseek-v4-pro"
                assert completed["usage"]["cache_read_tokens"] == 650000
                assert completed["usage"]["cache_write_tokens"] == 42


    @pytest.mark.asyncio
    async def test_approval_resolve_all_is_scoped_to_target_run(self, auth_adapter):
        """Same client session_id must not let one run approve another run's queue."""
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_create_agent") as mock_create:
                victim_agent, victim_ready, victim_interrupted = _make_slow_agent()
                attacker_agent, attacker_ready, attacker_interrupted = _make_slow_agent()
                mock_create.side_effect = [victim_agent, attacker_agent]

                victim_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "victim", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                attacker_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "attacker", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert victim_resp.status == 202
                assert attacker_resp.status == 202
                victim_run = (await victim_resp.json())["run_id"]
                attacker_run = (await attacker_resp.json())["run_id"]

                victim_ready.wait(timeout=3.0)
                attacker_ready.wait(timeout=3.0)
                assert auth_adapter._run_approval_sessions[victim_run] == victim_run
                assert auth_adapter._run_approval_sessions[attacker_run] == attacker_run
                assert auth_adapter._run_approval_sessions[victim_run] != auth_adapter._run_approval_sessions[attacker_run]

                victim_entry = approval_gateway_wait._ApprovalEntry({
                    "command": "bash -c victim-danger",
                    "description": "victim approval",
                    "pattern_keys": ["shell-c"],
                })
                attacker_entry = approval_gateway_wait._ApprovalEntry({
                    "command": "bash -c attacker-danger",
                    "description": "attacker approval",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[victim_run] = [victim_entry]
                    approval_mod._gateway_queues[attacker_run] = [attacker_entry]

                approval_resp = await cli.post(
                    f"/v1/runs/{attacker_run}/approval",
                    json={"choice": "always", "resolve_all": True},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
                assert approval_data["resolved"] == 1
                assert attacker_entry.result == "always"
                assert attacker_entry.event.is_set()
                assert victim_entry.result is None
                assert not victim_entry.event.is_set()
                with approval_mod._lock:
                    assert approval_mod._gateway_queues[victim_run] == [victim_entry]
                    assert victim_run in approval_mod._gateway_queues
                    assert attacker_run not in approval_mod._gateway_queues

                # Clean up the synthetic pending victim approval and unblock the
                # slow test agents so their background run tasks can finish.
                with approval_mod._lock:
                    approval_mod._gateway_queues.pop(victim_run, None)
                victim_interrupted.set()
                attacker_interrupted.set()


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/steer — steer a running agent
# ---------------------------------------------------------------------------


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_steer_running_agent(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        queue = asyncio.Queue()
        adapter._active_run_agents["run_123"] = agent
        adapter._run_streams["run_123"] = queue
        adapter._set_run_status("run_123", "running")
        _claim_run(adapter, "run_123")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": "tighten the ending"})
            payload = await resp.json()

        assert resp.status == 200
        assert payload == {
            "object": "hermes.run.steer",
            "run_id": "run_123",
            "accepted": True,
        }
        agent.steer.assert_called_once_with("tighten the ending")
        assert adapter._run_statuses["run_123"]["last_event"] == "run.steered"
        event = queue.get_nowait()
        assert event["event"] == "run.steered"
        assert event["run_id"] == "run_123"
        assert event["accepted"] is True

    @pytest.mark.asyncio
    async def test_steer_nonexistent_run_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_missing/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 404
        assert payload["error"]["code"] == "run_not_found"

    @pytest.mark.asyncio
    async def test_steer_inactive_run_returns_409(self, adapter):
        app = _create_runs_app(adapter)
        adapter._set_run_status("run_done", "completed")
        _claim_run(adapter, "run_done")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_done/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 409
        assert payload["error"]["code"] == "run_not_accepting_steer"

    @pytest.mark.asyncio
    async def test_steer_missing_input_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        adapter._active_run_agents["run_123"] = agent
        adapter._set_run_status("run_123", "running")
        _claim_run(adapter, "run_123")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": ""})
            payload = await resp.json()

        assert resp.status == 400
        assert payload["error"]["code"] == "invalid_steer_input"
        agent.steer.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_then_steer_rejects_retained_agent_ref(self, adapter):
        """Steer must reject a stopping run even if the executor thread is still live."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_started = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.steer = MagicMock(return_value=True)

                def _interrupt(_message=None):
                    return None

                def _run_conversation(*_args, **_kwargs):
                    run_started.set()
                    run_can_finish.wait(timeout=5)
                    return {"final_response": "late result"}

                mock_agent.interrupt = MagicMock(side_effect=_interrupt)
                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert run_started.wait(timeout=3.0)

                stop = asyncio.create_task(cli.post(f"/v1/runs/{run_id}/stop"))
                try:
                    for _ in range(100):
                        if adapter._run_statuses[run_id]["status"] == "stopping":
                            break
                        await asyncio.sleep(0.01)
                    assert adapter._run_statuses[run_id]["status"] == "stopping"
                    assert not stop.done()
                    assert run_id in adapter._active_run_agents

                    steer_resp = await cli.post(
                        f"/v1/runs/{run_id}/steer",
                        json={"input": "tighten the ending"},
                    )
                    steer_data = await steer_resp.json()

                    assert steer_resp.status == 409
                    assert steer_data["error"]["code"] == "run_not_accepting_steer"
                    mock_agent.steer.assert_not_called()
                finally:
                    run_can_finish.set()
                assert (await asyncio.wait_for(stop, 5)).status == 200
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_pending_steer_preserved_on_run_completed(self, adapter):
        """A steer drained by the turn finalizer (accepted after the final
        response) must surface as pending_steer on the terminal run status
        instead of being silently dropped."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.run_conversation.return_value = {
                    "final_response": "done",
                    "pending_steer": "tighten the ending",
                }
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]

                for _ in range(40):
                    status = adapter._run_statuses.get(run_id, {})
                    if status.get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert adapter._run_statuses[run_id]["status"] == "completed"
        assert adapter._run_statuses[run_id]["pending_steer"] == "tighten the ending"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected_status"),
        [
            ({"final_response": "Operation interrupted.", "interrupted": True, "completed": False}, "cancelled"),
            ({"final_response": "Budget exhausted summary.", "completed": False,
              "turn_exit_reason": "max_iterations_reached(2/2)"}, "failed"),
            ({"final_response": "done", "completed": True}, "completed"),
        ],
    )
    async def test_run_terminal_status_follows_result_flags(self, adapter, result, expected_status):
        """A turn that ended interrupted or unfinished must not be booked as ``completed``
        (#111770): the persisted status, the ``completed`` flag and the terminal event name
        agree, and a late steer survives on every terminal status."""
        result["pending_steer"] = "tighten the ending"
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.run_conversation.return_value = result
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                for _ in range(40):
                    if adapter._run_statuses.get(run_id, {}).get("status") == expected_status:
                        break
                    await asyncio.sleep(0.05)
                events = await (await cli.get(f"/v1/runs/{run_id}/events")).text()

        status = adapter._run_statuses[run_id]
        assert status["status"] == expected_status
        assert status["completed"] is (expected_status == "completed")
        assert status["pending_steer"] == "tighten the ending"
        assert f"run.{expected_status}" in events

    @pytest.mark.asyncio
    async def test_steer_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_any/steer", json={"input": "hello"})

        assert resp.status == 401


# ---------------------------------------------------------------------------
# Run lifecycle TTL sweeping
# ---------------------------------------------------------------------------


class TestRunLifecycleSweep:

    @pytest.mark.asyncio
    async def test_expired_live_run_drops_transport_but_keeps_control_state(self, adapter):
        """Stream TTL bounds buffering without detaching a live run."""
        app = _create_runs_app(adapter)
        adapter._max_concurrent_runs = 1

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert start_resp.status == 202
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                task = adapter._active_run_tasks[run_id]
                assert isinstance(task, asyncio.Task)
                assert not task.done()

                pending = approval_gateway_wait._ApprovalEntry({
                    "command": "bash -c long-running",
                    "description": "approval after stream TTL",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [pending]

                adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
                # Exercise one real sweeper iteration without waiting 60 seconds.
                with patch(
                    "gateway.platforms.api_server.asyncio.sleep",
                    side_effect=[None, asyncio.CancelledError()],
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await adapter._sweep_orphaned_runs()

                assert adapter._active_run_tasks[run_id] is task
                assert adapter._active_run_agents[run_id] is mock_agent
                assert run_id not in adapter._run_streams
                assert run_id not in adapter._run_streams_created
                assert adapter._run_approval_sessions[run_id] == run_id

                limited = adapter._concurrency_limited_response()
                assert limited is not None
                assert limited.status == 429

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once"},
                )
                assert approval_resp.status == 200
                assert pending.event.is_set()
                assert pending.result == "once"

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")


# ---------------------------------------------------------------------------
# Run ownership across served profiles (#93689 / #90415)
# ---------------------------------------------------------------------------


class TestRunOwnershipAcrossProfiles:
    """Every served profile holds a valid key under multiplex; only the
    creating profile may see or control a run."""

    KEYS = {"victim": "sk-victim-profile-key-0001", "attacker": "sk-attacker-profile-key-01"}

    @classmethod
    def _profile_app(cls, adapter: APIServerAdapter) -> web.Application:
        """Runs routes behind a stand-in for the /p/<profile>/ middleware:
        the routed profile arrives in ``X-Test-Profile`` and each profile
        authenticates with its own key, as under gateway.multiplex_profiles."""

        @web.middleware
        async def stamp_profile(request, handler):
            token = _api_request_profile.set(request.headers.get("X-Test-Profile"))
            try:
                return await handler(request)
            finally:
                _api_request_profile.reset(token)

        adapter._expected_api_key = lambda: cls.KEYS.get(_api_request_profile.get(), "")
        app = _create_runs_app(adapter)
        app.middlewares.append(stamp_profile)
        app.router.add_post(
            "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
        )
        return app

    @pytest.mark.asyncio
    async def test_unstamped_run_state_fails_closed(self, adapter):
        """Run state with no owner stamp is nobody's — not everybody's."""
        app = _create_runs_app(adapter)
        adapter._active_run_agents["run_unstamped"] = MagicMock()
        adapter._set_run_status("run_unstamped", "running")

        async with TestClient(TestServer(app)) as cli:
            get_resp = await cli.get("/v1/runs/run_unstamped")
            stop_resp = await cli.post("/v1/runs/run_unstamped/stop")

        assert (get_resp.status, stop_resp.status) == (404, 404)

    @pytest.mark.asyncio
    async def test_session_chat_stream_run_is_owned_by_creating_profile(self, adapter):
        """The session-chat-stream run mint claims ownership like /v1/runs does."""
        app = self._profile_app(adapter)
        victim = {"X-Test-Profile": "victim", "Authorization": f"Bearer {self.KEYS['victim']}"}
        attacker = {"X-Test-Profile": "attacker", "Authorization": f"Bearer {self.KEYS['attacker']}"}
        gate = asyncio.Event()

        async def slow_run_agent(**kwargs):
            await gate.wait()
            return {"final_response": "ok"}, {}

        async with TestClient(TestServer(app)) as cli:
            with (
                patch.object(adapter, "_get_existing_session_or_404", new=AsyncMock(return_value=({"id": "s1"}, None))),
                patch.object(adapter, "_conversation_history_for_session", new=AsyncMock(return_value=[])),
                patch.object(adapter, "_run_agent", new=slow_run_agent),
            ):
                stream = await cli.post(
                    "/api/sessions/s1/chat/stream", json={"message": "hi"}, headers=victim
                )
                await stream.content.readline()
                (run_id,) = list(adapter._run_statuses)
                assert run_id in adapter._run_owners

                foreign_get = await cli.get(f"/v1/runs/{run_id}", headers=attacker)
                foreign_stop = await cli.post(f"/v1/runs/{run_id}/stop", headers=attacker)
                own_get = await cli.get(f"/v1/runs/{run_id}", headers=victim)
                assert (foreign_get.status, foreign_stop.status, own_get.status) == (404, 404, 200)

                gate.set()
                await stream.text()

        # The owner outlives the terminal status and goes with the last surface.
        assert run_id in adapter._run_owners
        adapter._run_statuses.pop(run_id)
        adapter._release_run_owner_if_forgotten(run_id)
        assert run_id not in adapter._run_owners


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/stop — interrupt a running agent
# ---------------------------------------------------------------------------


class TestStopRun:

    @pytest.mark.asyncio
    async def test_stop_during_agent_construction_never_starts_turn(self, adapter):
        """Stop in the pre-registration gap must settle and never enter the executor."""
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.session_prompt_tokens = 0
        agent.session_completion_tokens = 0
        agent.session_total_tokens = 0
        stop_responses = []

        def create_agent(**_kwargs):
            # The synchronous factory runs on the event-loop thread. Issue the
            # authenticated stop on another thread at the exact pre-register gap.
            def stop_in_thread():
                request = MagicMock()
                request.headers = {}
                request.match_info = {"run_id": next(iter(adapter._active_run_tasks))}
                request.path = f"/v1/runs/{request.match_info['run_id']}/stop"
                stop_responses.append(asyncio.run(adapter._handle_stop_run(request)))

            thread = threading.Thread(target=stop_in_thread)
            thread.start()
            thread.join(timeout=3)
            assert not thread.is_alive()
            return agent

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", side_effect=create_agent):
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                for _ in range(40):
                    if adapter._run_statuses[run_id]["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.05)
                assert len(stop_responses) == 1
                assert stop_responses[0].status == 200
                assert agent.run_conversation.call_count == 0
                assert agent.close.call_count == 1
                assert agent.interrupt.call_count == 0
                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks
                status = await cli.get(f"/v1/runs/{run_id}")
                assert (await status.json())["status"] == "cancelled"
                again = await cli.post(f"/v1/runs/{run_id}/stop")
                assert (await again.json())["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_before_executor_entry_never_starts_turn(self, adapter):
        """The executor must honor a stop after the event-loop's final check."""
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.run_budget_seconds = 0

        def stop_at_executor_boundary(_agent, _task_id, *, session_key):
            request = MagicMock()
            request.headers = {}
            request.match_info = {"run_id": session_key}
            request.path = f"/v1/runs/{session_key}/stop"
            # Preserve real ownership/baseline setup to exercise stop reaping.
            _publish_turn_process_ownership(_agent, _task_id, session_key=session_key)
            response = asyncio.run(adapter._handle_stop_run(request))
            assert response.status == 200

        async with TestClient(TestServer(app)) as cli:
            with (
                patch.object(adapter, "_create_agent", return_value=agent),
                patch("gateway.platforms.api_server._publish_turn_process_ownership",
                      side_effect=stop_at_executor_boundary),
            ):
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                for _ in range(40):
                    if adapter._run_statuses[run_id]["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.05)
                status = await cli.get(f"/v1/runs/{run_id}")
                assert (await status.json())["status"] == "cancelled"
                agent.run_conversation.assert_not_called()
                assert run_id not in adapter._active_run_agents

    @pytest.mark.asyncio
    async def test_stop_at_final_check_before_method_entry_waits_for_turn(self, adapter):
        """Stop in the check/call gap cannot ACK before any turn hooks execute."""
        app = _create_runs_app(adapter)
        checked = threading.Event()
        allow_entry = threading.Event()
        turn_finished = threading.Event()
        agent = MagicMock()
        agent.run_budget_seconds = 0
        agent.session_prompt_tokens = 0
        agent.session_completion_tokens = 0
        agent.session_total_tokens = 0
        call_order = []

        class GatedEntry:
            def __getattribute__(self, name):
                if name == "run_conversation":
                    checked.set()
                    assert allow_entry.wait(timeout=5)
                    def run(**_kwargs):
                        call_order.append("entered")
                        turn_finished.set()
                        return {"final_response": "done"}
                    return run
                return super().__getattribute__(name)

            def __getattr__(self, name):
                return getattr(agent, name)

        # Delegate all gateway agent lifecycle methods to the mock, while
        # suspending method lookup exactly after the final stop check.
        gated = GatedEntry()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", return_value=gated):
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                try:
                    assert await asyncio.to_thread(checked.wait, 3)
                    stop = asyncio.create_task(cli.post(f"/v1/runs/{run_id}/stop"))
                    for _ in range(100):
                        if adapter._run_statuses[run_id]["status"] == "stopping":
                            break
                        await asyncio.sleep(0.01)
                    assert adapter._run_statuses[run_id]["status"] == "stopping"
                    assert not stop.done()
                    poll = await cli.get(f"/v1/runs/{run_id}")
                    assert (await poll.json())["status"] == "stopping"
                finally:
                    allow_entry.set()
                stopped = await asyncio.wait_for(stop, 5)
                assert stopped.status == 200
                assert turn_finished.is_set()
                assert call_order == ["entered"]
                for _ in range(100):
                    if adapter._run_statuses[run_id]["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert (await (await cli.get(f"/v1/runs/{run_id}")).json())["status"] == "completed"

    @pytest.mark.asyncio
    async def test_completion_wins_before_uncooperative_stop_is_acknowledged(
        self, adapter
    ):
        """A provisional Stop cannot discard a real completion."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_finished = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                started = threading.Event()

                def _run_conversation(*_args, **_kwargs):
                    started.set()
                    run_can_finish.wait(timeout=5)
                    run_finished.set()
                    return {"final_response": "late result"}

                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                assert started.wait(timeout=3)

                stop = asyncio.create_task(cli.post(f"/v1/runs/{run_id}/stop"))
                try:
                    for _ in range(100):
                        if adapter._run_statuses[run_id]["status"] == "stopping":
                            break
                        await asyncio.sleep(0.01)
                    assert not run_finished.is_set()
                    assert not stop.done()
                    assert run_id in adapter._active_run_agents
                    assert run_id in adapter._active_run_tasks
                    assert adapter._run_statuses[run_id]["status"] == "stopping"
                finally:
                    run_can_finish.set()
                stop_resp = await asyncio.wait_for(stop, 5)
                assert stop_resp.status == 200
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "completed"
                assert adapter._run_statuses[run_id]["output"] == "late result"

    @pytest.mark.asyncio
    async def test_uncooperative_turn_stop_times_out_without_retiring_live_run(self, adapter, monkeypatch):
        from gateway.platforms import api_server_runs

        monkeypatch.setattr(api_server_runs, "_RUN_STOP_ENTRY_WAIT_SECONDS", 0.05)
        release = threading.Event()
        entered = threading.Event()
        agent = MagicMock()
        agent.run_budget_seconds = 0
        agent.session_prompt_tokens = 0
        agent.session_completion_tokens = 0
        agent.session_total_tokens = 0

        def uncooperative_turn(**_kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return {"final_response": "late result"}

        agent.run_conversation.side_effect = uncooperative_turn
        try:
            async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
                with patch.object(adapter, "_create_agent", return_value=agent):
                    response = await cli.post("/v1/runs", json={"input": "hello"})
                    run_id = (await response.json())["run_id"]
                    assert await asyncio.to_thread(entered.wait, 3)
                    stopped = await asyncio.wait_for(cli.post(f"/v1/runs/{run_id}/stop"), 1)
                    assert (await stopped.json()) == {"run_id": run_id, "status": "stopping"}
                    assert (await (await cli.get(f"/v1/runs/{run_id}")).json())["status"] == "stopping"
                    assert run_id in adapter._stopping_run_ids
                    assert run_id in adapter._active_run_agents
                    assert run_id in adapter._active_run_tasks
                    assert agent.close.call_count == 0
                    release.set()
                    for _ in range(100):
                        if run_id not in adapter._active_run_tasks:
                            break
                        await asyncio.sleep(0.01)
                    assert run_id not in adapter._active_run_tasks
                    status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    assert status["status"] == "completed"
                    assert status["output"] == "late result"
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_stop_timeout_during_uncertain_entry_does_not_ack_cancelled(self, adapter, monkeypatch):
        from gateway.platforms import api_server_runs

        monkeypatch.setattr(api_server_runs, "_RUN_STOP_ENTRY_WAIT_SECONDS", 0.05)
        checked = threading.Event()
        release = threading.Event()
        entered = threading.Event()
        agent = MagicMock()
        agent.run_budget_seconds = 0
        agent.session_prompt_tokens = 0
        agent.session_completion_tokens = 0
        agent.session_total_tokens = 0

        class GatedEntry:
            def __getattribute__(self, name):
                if name == "run_conversation":
                    checked.set()
                    assert release.wait(timeout=5)
                    def turn(**_kwargs):
                        entered.set()
                        return {"final_response": "entered despite stop"}
                    return turn
                return super().__getattribute__(name)

            def __getattr__(self, name):
                return getattr(agent, name)

        try:
            async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
                with patch.object(adapter, "_create_agent", return_value=GatedEntry()):
                    response = await cli.post("/v1/runs", json={"input": "hello"})
                    run_id = (await response.json())["run_id"]
                    assert await asyncio.to_thread(checked.wait, 3)
                    stopped = await asyncio.wait_for(cli.post(f"/v1/runs/{run_id}/stop"), 1)
                    assert (await stopped.json())["status"] == "stopping"
                    assert not entered.is_set()
                    assert (await (await cli.get(f"/v1/runs/{run_id}")).json())["status"] == "stopping"
                    assert run_id in adapter._active_run_tasks
                    release.set()
                    assert await asyncio.to_thread(entered.wait, 3)
                    for _ in range(100):
                        if run_id not in adapter._active_run_tasks:
                            break
                        await asyncio.sleep(0.01)
                    status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    assert status["status"] == "completed"
                    assert status["output"] == "entered despite stop"
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_stop_running_agent(self, adapter):
        """Stop should interrupt the agent and cancel the task."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Wait for agent to start running in the thread
                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Verify agent ref is stored
                assert run_id in adapter._active_run_agents

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["run_id"] == run_id
                assert stop_data["status"] == "stopping"

                # Agent interrupt should have been called
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                assert status_resp.status == 200
                status_data = await status_resp.json()
                assert status_data["status"] in {"stopping", "cancelled"}

                # Refs should be cleaned up
                await asyncio.sleep(0.2)
                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks


    @pytest.mark.asyncio
    async def test_stop_sends_sentinel_to_events_stream(self, adapter):
        """After stop, the events stream should close."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Subscribe to events in background
                events_task = asyncio.ensure_future(
                    cli.get(f"/v1/runs/{run_id}/events")
                )

                await asyncio.sleep(0.1)

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200

                # Events stream should close
                events_resp = await asyncio.wait_for(events_task, timeout=5.0)
                assert events_resp.status == 200
                body = await events_resp.text()
                # Stream should have received run.failed and closed
                assert "run.failed" in body or "stream closed" in body


class TestRunsProviderAuthFailure:
    @pytest.mark.asyncio
    async def test_status_reports_provider_auth_failure_distinctly(self, adapter):
        """/v1/runs builds its own agent via _create_agent() and does not
        route through _run_agent(), so the controlled "Provider
        authentication failed" message added there does not cover this
        endpoint. _handle_runs()'s own _ProviderAuthResolutionError branch
        must give the same distinguished message instead of the generic
        except-Exception "run failed" text."""
        from gateway.platforms.api_server import _ProviderAuthResolutionError

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.side_effect = _ProviderAuthResolutionError(
                    "No credentials found for provider 'nous'"
                )

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "failed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "failed"
                assert (
                    status["error"]
                    == "⚠️ Provider authentication failed: No credentials found for provider 'nous'"
                )
                assert status["last_event"] == "run.failed"


# ---------------------------------------------------------------------------
# POST /v1/runs idempotency
# ---------------------------------------------------------------------------


def _use_idempotency_db(adapter, path):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    adapter._run_idempotency_store.close()
    adapter._run_idempotency_store = RunIdempotencyStore(str(path))


class TestWorkflowAdmissionStaging:
    def test_protected_profiles_are_explicit_server_configuration(self):
        configured = APIServerAdapter(PlatformConfig(enabled=True, extra={
            "workflow_protected_profiles": ["crm"]}))
        assert configured._workflow_protected_profiles == frozenset({"crm"})
        assert _make_adapter()._workflow_protected_profiles == frozenset()
        with pytest.raises(ValueError):
            APIServerAdapter(PlatformConfig(enabled=True, extra={
                "workflow_protected_profiles": "crm"}))

    @pytest.fixture(autouse=True)
    def protected_profile(self, adapter):
        adapter._workflow_protected_profiles = frozenset({"default"})

    @pytest.mark.asyncio
    async def test_protected_profile_denies_generic_manager_bearer_and_readiness_is_local(
        self, adapter, tmp_path, monkeypatch
    ):
        _use_idempotency_db(adapter, tmp_path / "runs.db")
        adapter._api_key = "manager-key"
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                denied = await cli.post("/v1/runs", json={"input": "bypass"},
                    headers={"Authorization": "Bearer manager-key"})
                assert denied.status == 403
                assert (await denied.json())["error"]["code"] == "workflow_ticket_required"
                create.assert_not_called()
            url = "/v1/workflow-profile/readiness"
            assert (await cli.get(url, headers={"Authorization": "Bearer manager-key"})).status == 401
            ready = await cli.get(url, headers={"Authorization": "Bearer service-secret"})
            assert ready.status == 200
            assert await ready.json() == {
                "profile": "default", "protected": True, "workflow_admission_enabled": True,
                "generic_runs_denied": True}
            monkeypatch.delenv("SLC_ATTEMPT_VERIFY_TOKEN")
            not_ready = await cli.get(url, headers={"Authorization": "Bearer service-secret"})
            assert (await not_ready.json())["workflow_admission_enabled"] is False
            assert (await cli.post("/v1/runs", json={"input": "bypass"},
                headers={"Authorization": "Bearer manager-key"})).status == 403

    @pytest.mark.asyncio
    async def test_workflow_ticket_fails_closed_without_slc_capability(
        self, adapter, tmp_path, monkeypatch
    ):
        from gateway.platforms import api_server_runs
        _use_idempotency_db(adapter, tmp_path / "runs.db")
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        raw = b'{"input":"do work"}'
        ticket, _ = self._ticket(raw)
        with patch.object(api_server_runs, "_workflow_capable", AsyncMock(return_value=False)):
            async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
                response = await cli.post("/v1/runs", data=raw, headers={
                    "Authorization": "Bearer service-secret", "X-Hermes-Workflow-Ticket": ticket,
                    "Idempotency-Key": "ticket-key"})
                assert response.status == 503
                assert adapter._run_idempotency_store.workflow_registry("attempt-1") is None

    @staticmethod
    def _ticket(raw_body: bytes, *, profile="default", key="ticket-key", secret="signing-secret"):
        claims = {
            "v": 1, "aud": "Hermes workflow", "attempt_id": "attempt-1",
            "task_id": "task-1", "assignee": "agent-1", "profile": profile,
            "queue_ready_event_id": "event-1", "payload_sha256": hashlib.sha256(raw_body).hexdigest(),
            "transport_key": key, "attempt_no": 1,
            "issued_at": int(time.time()) - 1, "expires_at": int(time.time()) + 120,
        }
        segment = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(
            secret.encode(), ("slc-attempt-v1." + segment).encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return f"{segment}.{signature}", claims

    @pytest.mark.asyncio
    async def test_abort_receipt_fences_post_and_survives_restart(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
        from gateway.platforms import api_server_runs
        path = tmp_path / "runs.db"
        _use_idempotency_db(adapter, path)
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        capable = AsyncMock(return_value=False)
        monkeypatch.setattr(api_server_runs, "_workflow_capable", capable)
        raw = b'{"input":"do work"}'
        ticket, claims = self._ticket(raw)
        auth = {"Authorization": "Bearer service-secret"}
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            url = "/v1/workflow-attempts/attempt-1"
            assert (await cli.post(url + "/abort", json={"ticket": ticket})).status == 401
            assert (await cli.post(url + "/abort", headers=auth, json={"ticket": ticket + "x"})).status == 401
            # Invalid base64 length is a bad ticket, not a service outage.
            assert (await cli.post(url + "/abort", headers=auth, json={"ticket": "A.A"})).status == 401
            assert (await cli.post("/v1/workflow-attempts/other/abort", headers=auth,
                                   json={"ticket": ticket})).status == 401
            assert (await cli.get(url)).status == 401
            response = await cli.post(url + "/abort", headers=auth, json={"ticket": ticket})
            assert response.status == 200
            receipt = await response.json()
            assert {k: receipt[k] for k in (
                "attempt_id", "task_id", "assignee", "profile", "queue_ready_event_id", "attempt_no",
                "payload_sha256", "transport_key")} == {k: claims[k] for k in (
                "attempt_id", "task_id", "assignee", "profile", "queue_ready_event_id", "attempt_no",
                "payload_sha256", "transport_key")}
            assert receipt["status"] == "aborted" and receipt["active"] is False
            assert receipt["run_id"] is None and receipt["aborted"] is True
            assert receipt["abort_receipt"].startswith("abort_")
            capable.assert_not_awaited()  # abort must work while SLC's admission capability is absent
            capable.return_value = True
            repeat = await cli.post(url + "/abort", headers=auth, json={"ticket": ticket})
            assert repeat.status == 200 and await repeat.json() == receipt
            assert await (await cli.get(url, headers=auth)).json() == receipt
            with patch.object(adapter, "_create_agent") as create:
                post = await cli.post("/v1/runs", data=raw, headers={
                    **auth, "X-Hermes-Workflow-Ticket": ticket, "Idempotency-Key": "ticket-key"})
                assert post.status == 409
                create.assert_not_called()
            reopened = RunIdempotencyStore(str(path))
            try:
                assert reopened.workflow_registry("attempt-1") == receipt
                assert reopened.lookup_workflow("task-1", 1, "attempt-1",
                    api_server_runs._workflow_scope("default"),
                    api_server_runs._workflow_fingerprint(claims), "ticket-key")[0] == "aborted"
                assert reopened.abort_workflow({k: receipt[k] for k in (
                    "attempt_id", "task_id", "assignee", "profile", "queue_ready_event_id", "attempt_no",
                    "payload_sha256", "transport_key")}, api_server_runs._workflow_scope("default"),
                    api_server_runs._workflow_fingerprint(claims)) == ("aborted", receipt)
            finally:
                reopened.close()
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_run_binding").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM workflow_abort").fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_expired_ticket_can_abort_but_not_admit(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms import api_server_runs
        _use_idempotency_db(adapter, tmp_path / "runs.db")
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        raw = b'{"input":"do work"}'
        ticket, claims = self._ticket(raw)
        claims["issued_at"] = int(time.time()) - 120
        claims["expires_at"] = int(time.time()) - 1
        segment = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(
            b"signing-secret", ("slc-attempt-v1." + segment).encode(), hashlib.sha256).digest()).decode().rstrip("=")
        expired = segment + "." + signature
        with pytest.raises(ValueError):
            api_server_runs._decode_ticket(expired, secret="signing-secret", raw_body=raw,
                                           profile="default", transport_key="ticket-key")
        async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
            response = await cli.post("/v1/workflow-attempts/attempt-1/abort",
                headers={"Authorization": "Bearer service-secret"}, json={"ticket": expired})
            assert response.status == 200
            assert (await response.json())["status"] == "aborted"

    def test_abort_and_admission_race_is_serialized_across_connections(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
        from gateway.platforms.api_server_runs import _workflow_fingerprint, _workflow_scope
        path = str(tmp_path / "runs.db")
        stores = [RunIdempotencyStore(path), RunIdempotencyStore(path)]
        raw = b'{"input":"do work"}'
        _, claims = self._ticket(raw)
        registry = {k: claims[k] for k in (
            "attempt_id", "task_id", "assignee", "profile", "queue_ready_event_id", "attempt_no",
            "payload_sha256", "transport_key")}
        scope, fingerprint = _workflow_scope("default"), _workflow_fingerprint(claims)
        status = {"run_id": "run_1", "status": "held"}
        admitted = dict(registry, run_id="run_1", status="held", active=True)
        barrier = threading.Barrier(2)
        def post():
            barrier.wait()
            return stores[0].reserve_workflow("task-1", 1, scope, fingerprint, "run_1", status,
                                              admitted, owner_pid=1, owner_started=1)
        def abort():
            barrier.wait()
            return stores[1].abort_workflow(registry, scope, fingerprint)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                p, a = pool.submit(post), pool.submit(abort)
                post_result, abort_result = p.result(), a.result()
            assert (post_result[0], abort_result[0]) in {
                ("created", "admitted"), ("aborted", "aborted")}
            reopened = RunIdempotencyStore(path)
            try:
                receipt = reopened.workflow_registry("attempt-1")
                if post_result[0] == "created":
                    assert receipt["run_id"] == "run_1"
                    assert abort_result[1]["run_id"] == "run_1"
                else:
                    assert receipt["run_id"] is None and receipt["abort_receipt"]
                    assert reopened.reserve_workflow("task-1", 1, scope, fingerprint, "run_2", status,
                        admitted, owner_pid=1, owner_started=1)[0] == "aborted"
            finally:
                reopened.close()
        finally:
            for store in stores:
                store.close()

    @pytest.mark.asyncio
    async def test_unknown_post_never_reserves_or_starts_even_with_valid_ticket(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms.api_server_runs import _decode_ticket
        path = tmp_path / "runs.db"
        _use_idempotency_db(adapter, path)
        raw = b'{"input":"do work"}'
        ticket, claims = self._ticket(raw)
        assert _decode_ticket(ticket, secret="signing-secret", raw_body=raw,
                              profile="default", transport_key="ticket-key") == claims
        with pytest.raises(ValueError):
            _decode_ticket(ticket, secret="signing-secret", raw_body=b'{"input":"tampered"}',
                           profile="default", transport_key="ticket-key")
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                headers = {"X-Hermes-Workflow-Ticket": ticket, "Idempotency-Key": "ticket-key",
                           "Authorization": "Bearer service-secret"}
                for _ in range(2):
                    response = await cli.post("/v1/runs", data=raw, headers=headers)
                    assert response.status == 503
                    assert (await response.json())["error"]["code"] == "workflow_admission_unavailable"
                create.assert_not_called()
                registry = await cli.get("/v1/workflow-attempts/attempt-1",
                                         headers={"Authorization": "Bearer service-secret"})
                assert registry.status == 404
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_run_binding").fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_capability_hold_bind_release_and_terminal_registry(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms import api_server_runs
        path = tmp_path / "runs.db"
        _use_idempotency_db(adapter, path)
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        capability = False
        released = asyncio.Event()
        async def capable(url, token):
            assert url == "http://slc.invalid" and token == "verify-secret"
            return capability
        async def wait(claims, run_id, url, token, stopped):
            await released.wait()
            return not stopped()
        monkeypatch.setattr(api_server_runs, "_workflow_capable", capable)
        monkeypatch.setattr(api_server_runs, "_wait_for_workflow_release", wait)
        raw = b'{"input":"do work"}'
        ticket, claims = self._ticket(raw)
        headers = {"X-Hermes-Workflow-Ticket": ticket, "Idempotency-Key": "ticket-key",
                   "Authorization": "Bearer service-secret"}
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                rejected = await cli.post("/v1/runs", data=raw, headers=headers)
                assert rejected.status == 503
                assert adapter._run_idempotency_store.workflow_registry("attempt-1") is None
                capability = True
                first = await cli.post("/v1/runs", data=raw, headers=headers)
                assert first.status == 202
                first_body = await first.json()
                assert first_body["status"] == "held"
                run_id = first_body["run_id"]
                replay = await cli.post("/v1/runs", data=raw, headers=headers)
                assert (await replay.json())["run_id"] == run_id
                assert replay.headers["Idempotency-Replayed"] == "true"
                renewed, _ = self._ticket(raw)
                renewed_resp = await cli.post("/v1/runs", data=raw, headers={
                    **headers, "X-Hermes-Workflow-Ticket": renewed})
                assert (await renewed_resp.json())["run_id"] == run_id
                registry = await cli.get("/v1/workflow-attempts/attempt-1",
                                         headers={"Authorization": "Bearer service-secret"})
                held = await registry.json()
                assert registry.status == 200
                assert all(held[k] == claims[k] for k in (
                    "attempt_id", "task_id", "assignee", "profile", "queue_ready_event_id", "attempt_no"))
                assert held["run_id"] == run_id and held["status"] == "held" and held["active"] is True
                abort_response = await cli.post("/v1/workflow-attempts/attempt-1/abort",
                    headers={"Authorization": "Bearer service-secret"}, json={"ticket": ticket})
                assert abort_response.status == 409
                conflict = await abort_response.json()
                assert conflict["run_id"] == run_id and conflict["status"] == "held"
                assert (await cli.get("/v1/workflow-attempts/attempt-1",
                    headers={"Authorization": "Bearer service-secret"})).status == 200
                assert adapter._run_idempotency_store.workflow_registry("attempt-1")["run_id"] == run_id
                create.assert_not_called()
                reopened = type(adapter._run_idempotency_store)(str(path))
                assert reopened.workflow_registry("attempt-1") == held
                reopened.close()
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
                create.return_value = agent
                released.set()
                for _ in range(100):
                    if adapter._run_idempotency_store.workflow_registry("attempt-1")["active"] is False:
                        break
                    await asyncio.sleep(.01)
                terminal = adapter._run_idempotency_store.workflow_registry("attempt-1")
                assert terminal["status"] == "completed" and terminal["active"] is False
                create.assert_called_once()
                agent.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_replay_after_restart_keeps_crashed_held_binding_active(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms import api_server_runs
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
        path = tmp_path / "runs.db"
        _use_idempotency_db(adapter, path)
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        monkeypatch.setattr(api_server_runs, "_workflow_capable", AsyncMock(return_value=True))
        release = asyncio.Event()
        async def held(*args):
            await release.wait()
            return False
        monkeypatch.setattr(api_server_runs, "_wait_for_workflow_release", held)
        raw = b'{"input":"do work"}'
        ticket, claims = self._ticket(raw)
        headers = {"Authorization": "Bearer service-secret", "X-Hermes-Workflow-Ticket": ticket,
                   "Idempotency-Key": "ticket-key"}
        try:
            async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
                admitted = await cli.post("/v1/runs", data=raw, headers=headers)
                run_id = (await admitted.json())["run_id"]
                reopened = RunIdempotencyStore(str(path))
                try:
                    assert reopened.workflow_registry("attempt-1")["active"] is True
                    assert reopened.lookup_workflow("task-1", 1, "attempt-1",
                        api_server_runs._workflow_scope("default"),
                        api_server_runs._workflow_fingerprint(claims), "ticket-key")[1]["run_id"] == run_id
                finally:
                    reopened.close()
                replay = await cli.post("/v1/runs", data=raw, headers=headers)
                assert replay.status == 202
                assert (await replay.json())["run_id"] == run_id
                assert replay.headers["Idempotency-Replayed"] == "true"
                # A crash cannot prove descendant cleanup: interrupted is still active.
                store = RunIdempotencyStore(str(path))
                try:
                    status = store.workflow_status_for_run(api_server_runs._workflow_scope("default"), run_id)
                    status["status"]["status"] = "interrupted"
                    store.update_workflow_status(run_id, status["status"], active=True)
                    assert store.workflow_registry("attempt-1")["active"] is True
                finally:
                    store.close()
        finally:
            release.set()
            for task in list(adapter._active_run_tasks.values()):
                await task

    @pytest.mark.asyncio
    async def test_stop_held_run_never_starts_agent(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms import api_server_runs
        _use_idempotency_db(adapter, tmp_path / "runs.db")
        monkeypatch.setenv("HERMES_WORKFLOW_SERVICE_KEY", "service-secret")
        monkeypatch.setenv("SLC_ATTEMPT_SIGNING_SECRET", "signing-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_TOKEN", "verify-secret")
        monkeypatch.setenv("SLC_ATTEMPT_VERIFY_URL", "http://slc.invalid")
        monkeypatch.setattr(api_server_runs, "_workflow_capable", AsyncMock(return_value=True))
        entered = asyncio.Event()
        async def held(claims, run_id, url, token, stopped):
            entered.set()
            while not stopped():
                await asyncio.sleep(.01)
            return False
        monkeypatch.setattr(api_server_runs, "_wait_for_workflow_release", held)
        raw = b'{"input":"do work"}'
        ticket, _ = self._ticket(raw)
        headers = {"X-Hermes-Workflow-Ticket": ticket, "Idempotency-Key": "ticket-key",
                   "Authorization": "Bearer service-secret"}
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                response = await cli.post("/v1/runs", data=raw, headers=headers)
                run_id = (await response.json())["run_id"]
                await entered.wait()
                stopped = await cli.post(f"/v1/runs/{run_id}/stop", headers={
                    "Authorization": "Bearer service-secret"})
                assert stopped.status == 200
                for _ in range(100):
                    if adapter._run_idempotency_store.workflow_registry("attempt-1")["active"] is False:
                        break
                    await asyncio.sleep(.01)
                registry = adapter._run_idempotency_store.workflow_registry("attempt-1")
                assert registry["status"] == "cancelled" and registry["active"] is False
                create.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_get_requires_exact_started_binding(self, monkeypatch):
        from gateway.platforms.api_server_runs import _wait_for_workflow_release, _workflow_capable
        attempts = []
        admitted = False
        async def capability(request):
            assert request.headers["Authorization"] == "Bearer verify-secret"
            return web.json_response({"admission_ready": admitted})
        async def verify(request):
            assert request.query["run_id"] == "run_1"
            assert request.headers["Authorization"] == "Bearer verify-secret"
            attempts.append(1)
            if len(attempts) == 1:
                return web.json_response({"admitted": True, "attempt_id": "attempt-1",
                                          "task_id": "task-1", "assignee": "other", "profile": "default",
                                          "run_id": "run_1", "start_event_id": "start-1"})
            if len(attempts) == 2:
                return web.json_response({"admitted": False}, status=409)
            return web.json_response({"admitted": True, "attempt_id": "attempt-1",
                                      "task_id": "task-1", "assignee": "agent-1", "profile": "default",
                                      "run_id": "run_1", "start_event_id": "start-1"})
        app = web.Application()
        app.router.add_get("/internal/task-attempts/capabilities", capability)
        app.router.add_get("/internal/task-attempts/{attempt_id}/admission", verify)
        async with TestServer(app) as server:
            base = str(server.make_url("/" )).rstrip("/")
            assert not await _workflow_capable(base, "verify-secret")
            admitted = True
            assert await _workflow_capable(base, "verify-secret")
            claims = {"attempt_id": "attempt-1", "task_id": "task-1", "assignee": "agent-1",
                      "profile": "default"}
            assert await _wait_for_workflow_release(claims, "run_1", base, "verify-secret", lambda: False)
            assert len(attempts) == 3

    def test_workflow_binding_is_durable_unique_and_fails_closed(self, tmp_path):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
        path = tmp_path / "runs.db"
        status = {"run_id": "run_1", "status": "held"}
        registry = {"attempt_id": "attempt-1", "task_id": "task-1", "attempt_no": 1,
                    "assignee": "agent-1", "profile": "default", "queue_ready_event_id": "event-1",
                    "payload_sha256": "a" * 64, "transport_key": "key", "run_id": "run_1",
                    "status": "held", "active": True}
        store = RunIdempotencyStore(str(path))
        assert store.reserve_workflow("task-1", 1, "scope", "fingerprint", "run_1", status,
                                      registry, owner_pid=123, owner_started=456)[0] == "created"
        assert store.reserve_workflow("task-1", 1, "scope", "fingerprint", "run_2", status,
                                      registry, owner_pid=123, owner_started=456)[0] == "reused"
        assert store.reserve_workflow("task-1", 1, "scope", "different", "run_2", status,
                                      registry, owner_pid=123, owner_started=456)[0] == "conflict"
        other = dict(registry, attempt_id="attempt-2", run_id="run_2")
        assert store.reserve_workflow("task-1", 1, "scope", "fingerprint", "run_2", status,
                                      other, owner_pid=123, owner_started=456)[0] == "conflict"
        assert store.lookup_workflow("task-1", 1, "attempt-2", "scope", "fingerprint")[0] == "conflict"
        store.close()
        reopened = RunIdempotencyStore(str(path))
        assert reopened.workflow_registry("attempt-1") == registry
        assert reopened.lookup_workflow("task-1", 1, "attempt-1", "scope", "fingerprint")[1]["run_id"] == "run_1"
        reopened.close()
        memory = RunIdempotencyStore(":memory:")
        with pytest.raises(RuntimeError, match="durable"):
            memory.reserve_workflow("task-1", 1, "scope", "fingerprint", "run_1", status,
                                    registry, owner_pid=1, owner_started=1)
        with pytest.raises(RuntimeError, match="durable"):
            memory.abort_workflow(registry, "scope", "fingerprint")
        memory.close()


class TestRunIdempotency:
    @pytest.mark.asyncio
    async def test_invalid_body_does_not_consume_idempotency_key(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {"Idempotency-Key": "valid-after-rejection"}
            rejected = await cli.post(
                "/v1/runs", json={"input": ""}, headers=headers
            )
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                accepted = await cli.post(
                    "/v1/runs", json={"input": "valid"}, headers=headers
                )
        assert rejected.status == 400
        assert accepted.status == 202

    @pytest.mark.asyncio
    async def test_capacity_rejection_does_not_reserve_key(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        with patch.object(
            adapter,
            "_concurrency_limited_response",
            side_effect=[
                web.json_response({"error": "full"}, status=429),
                None,
            ],
        ):
            async with TestClient(TestServer(app)) as cli:
                headers = {"Idempotency-Key": "capacity-retry"}
                rejected = await cli.post(
                    "/v1/runs", json={"input": "valid"}, headers=headers
                )
                with patch.object(adapter, "_create_agent") as create:
                    agent = MagicMock()
                    agent.run_conversation.return_value = {"final_response": "done"}
                    agent.session_prompt_tokens = agent.session_completion_tokens = (
                        agent.session_total_tokens
                    ) = 0
                    create.return_value = agent
                    accepted = await cli.post(
                        "/v1/runs", json={"input": "valid"}, headers=headers
                    )
        assert rejected.status == 429
        assert accepted.status == 202

    @pytest.mark.asyncio
    async def test_sequential_duplicate_reuses_original(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        calls = 0
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()

                def run(**kwargs):
                    nonlocal calls
                    calls += 1
                    return {"final_response": "done"}

                agent.run_conversation.side_effect = run
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {"Idempotency-Key": "retry-1"}
                first = await cli.post(
                    "/v1/runs", json={"input": "hello"}, headers=headers
                )
                second = await cli.post(
                    "/v1/runs", json={"input": "hello"}, headers=headers
                )
                assert first.status == second.status == 202
                assert (await first.json())["run_id"] == (await second.json())["run_id"]
                assert second.headers["Idempotency-Replayed"] == "true"
                await asyncio.sleep(0.1)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_changed_payload_conflicts(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {"Idempotency-Key": "same-key"}
                assert (
                    await cli.post("/v1/runs", json={"input": "one"}, headers=headers)
                ).status == 202
                conflict = await cli.post(
                    "/v1/runs", json={"input": "two"}, headers=headers
                )
                assert conflict.status == 409
                assert (await conflict.json())["error"][
                    "code"
                ] == "idempotency_key_conflict"

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_starts_once(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        calls = 0
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()

                def run(**kwargs):
                    nonlocal calls
                    calls += 1
                    time.sleep(0.05)
                    return {"final_response": "done"}

                agent.run_conversation.side_effect = run
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent

                async def post():
                    response = await cli.post(
                        "/v1/runs",
                        json={"input": "race"},
                        headers={"Idempotency-Key": "race-key"},
                    )
                    return response.status, await response.json()

                results = await asyncio.gather(*[post() for _ in range(8)])
                assert {status for status, _ in results} == {202}
                assert len({body["run_id"] for _, body in results}) == 1
                await asyncio.sleep(0.15)
        assert calls == 1

    def test_restart_durability_and_terminal_semantics(self, tmp_path):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        path = tmp_path / "idem.db"
        for terminal in ("completed", "failed", "cancelled"):
            first = RunIdempotencyStore(str(path))
            run_id = f"run_{terminal}"
            assert (
                first.reserve(
                    "tenant",
                    terminal,
                    "fp",
                    run_id,
                    {"run_id": run_id, "status": terminal},
                )[0]
                == "created"
            )
            first.close()
            restarted = RunIdempotencyStore(str(path))
            outcome, record = restarted.reserve(
                "tenant", terminal, "fp", "run_new", {"status": "queued"}
            )
            assert outcome == "reused"
            assert record["run_id"] == run_id
            assert record["status"]["status"] == terminal
            restarted.close()

    def test_tenant_isolation_and_retention(self, tmp_path):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        store = RunIdempotencyStore(str(tmp_path / "idem.db"))
        assert (
            store.reserve("tenant-a", "key", "fp-a", "run_a", {"status": "queued"})[0]
            == "created"
        )
        assert (
            store.reserve("tenant-b", "key", "fp-b", "run_b", {"status": "queued"})[0]
            == "created"
        )
        store.close()

    def test_retention_never_releases_an_active_idempotency_reservation(
        self, tmp_path
    ):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        store = RunIdempotencyStore(str(tmp_path / "idem.db"))
        with patch("gateway.platforms.api_server.time.time", return_value=100):
            assert store.reserve(
                "tenant",
                "active-key",
                "active-fingerprint",
                "run-active",
                {"status": "running"},
            )[0] == "created"
            assert store.reserve(
                "tenant",
                "done-key",
                "done-fingerprint",
                "run-done",
                {"status": "completed"},
            )[0] == "created"

        after_retention = 100 + RunIdempotencyStore.RETENTION_SECONDS + 1
        with patch(
            "gateway.platforms.api_server.time.time", return_value=after_retention
        ):
            active, active_record = store.lookup(
                "tenant", "active-key", "active-fingerprint"
            )
            done, done_record = store.lookup(
                "tenant", "done-key", "done-fingerprint"
            )

        assert active == "reused"
        assert active_record["run_id"] == "run-active"
        assert done == "missing"
        assert done_record is None
        store.close()

    def test_room_terminal_receipt_survives_offline_home_until_grant_horizon(
        self, tmp_path, monkeypatch
    ):
        from gateway.platforms import api_server_run_idempotency as idempotency

        now = [100.0]
        monkeypatch.setattr(idempotency.time, "time", lambda: now[0])
        store = idempotency.RunIdempotencyStore(str(tmp_path / "idem.db"))
        horizon = now[0] + 30 * 24 * 60 * 60
        assert store.reserve(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
            "run-room",
            {"run_id": "run-room", "status": "completed"},
            retention_until=horizon,
        )[0] == "created"

        now[0] += idempotency.RunIdempotencyStore.RETENTION_SECONDS + 1
        store.reserve(
            "other-scope",
            "other-key",
            "other-fingerprint",
            "run-other",
            {"run_id": "run-other", "status": "queued"},
        )
        outcome, record = store.lookup(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
        )
        assert outcome == "reused"
        assert record["run_id"] == "run-room"

        now[0] = horizon + 1
        store.reserve(
            "third-scope",
            "third-key",
            "third-fingerprint",
            "run-third",
            {"run_id": "run-third", "status": "queued"},
        )
        assert store.lookup(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
        ) == ("missing", None)
        store.close()

    @pytest.mark.asyncio
    async def test_missing_key_preserves_legacy_new_run_behavior(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                first = await cli.post("/v1/runs", json={"input": "hello"})
                second = await cli.post("/v1/runs", json={"input": "hello"})
                assert (await first.json())["run_id"] != (await second.json())["run_id"]

    @pytest.mark.asyncio
    async def test_memory_scope_participates_in_fingerprint(
        self, auth_adapter, tmp_path
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                first_headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "memory-scope",
                    "X-Hermes-Session-Key": "memory-a",
                }
                second_headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "memory-scope",
                    "X-Hermes-Session-Key": "memory-b",
                }
                first = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=first_headers
                )
                conflict = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=second_headers
                )
                assert first.status == 202
                assert conflict.status == 409

    @pytest.mark.asyncio
    async def test_replay_bypasses_concurrency_limit_and_preserves_session_header(
        self, auth_adapter, tmp_path
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "lost-acceptance",
                    "X-Hermes-Session-Key": "memory-a",
                }
                first = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=headers
                )
                first_body = await first.json()
                with patch.object(
                    adapter,
                    "_concurrency_limited_response",
                    return_value=web.json_response({"error": "full"}, status=429),
                ):
                    replay = await cli.post(
                        "/v1/runs", json={"input": "same"}, headers=headers
                    )
                replay_body = await replay.json()
                assert replay.status == 202
                assert replay_body["run_id"] == first_body["run_id"]
                assert replay_body["replayed"] is True
                assert replay.headers["X-Hermes-Session-Key"] == "memory-a"

    @pytest.mark.asyncio
    async def test_direct_status_hydrates_after_adapter_restart(
        self, tmp_path
    ):
        path = tmp_path / "idem.db"
        first_adapter = _make_adapter()
        _use_idempotency_db(first_adapter, path)
        first_app = _create_runs_app(first_adapter)
        async with TestClient(TestServer(first_app)) as cli:
            with patch.object(first_adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                started = await cli.post(
                    "/v1/runs",
                    json={"input": "same"},
                    headers={"Idempotency-Key": "restart-status"},
                )
                run_id = (await started.json())["run_id"]
                for _ in range(40):
                    status = await cli.get(f"/v1/runs/{run_id}")
                    if (await status.json()).get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)
        first_adapter._run_idempotency_store.close()

        restarted = _make_adapter()
        _use_idempotency_db(restarted, path)
        restarted_app = _create_runs_app(restarted)
        async with TestClient(TestServer(restarted_app)) as cli:
            status = await cli.get(f"/v1/runs/{run_id}")
            body = await status.json()
        assert status.status == 200
        assert body["status"] == "completed"
        assert body["output"] == "done"

    @pytest.mark.asyncio
    async def test_dead_owner_nonterminal_status_becomes_interrupted(
        self, tmp_path
    ):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        path = tmp_path / "idem.db"
        scope = hashlib.sha256(
            "default\0unauthenticated-test-listener".encode()
        ).hexdigest()
        store = RunIdempotencyStore(str(path))
        store.reserve(
            scope,
            "stale-run",
            "fingerprint",
            "run_stale",
            {"run_id": "run_stale", "status": "running"},
            owner_pid=999_999_999,
            owner_started=1,
        )
        store.close()

        restarted = _make_adapter()
        _use_idempotency_db(restarted, path)
        app = _create_runs_app(restarted)
        async with TestClient(TestServer(app)) as cli:
            response = await cli.get("/v1/runs/run_stale")
            body = await response.json()
        assert response.status == 200
        assert body["status"] == "interrupted"
        assert body["last_event"] == "run.interrupted"


    def test_status_sweep_prunes_in_memory_ownership_mirrors(self, adapter):
        adapter._run_statuses["run_old"] = {
            "status": "completed",
            "updated_at": 1,
        }
        adapter._run_idempotency_ids.add("run_old")
        adapter._run_owners["run_old"] = "scope"

        adapter._sweep_orphaned_runs_once(adapter._RUN_STATUS_TTL + 2)

        assert "run_old" not in adapter._run_statuses
        assert "run_old" not in adapter._run_idempotency_ids
        assert "run_old" not in adapter._run_owners



class TestHostedRoomRuns:
    @pytest.mark.asyncio
    async def test_room_approval_requires_and_resolves_exact_request_id(
        self, auth_adapter
    ):
        run_id = "run-room-approval"
        current = approval_gateway_wait._ApprovalEntry({
            "request_id": "approval-B",
            "command": "rm -rf build-B",
        })
        auth_adapter._run_approval_sessions[run_id] = run_id
        auth_adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "waiting_for_approval",
            "approval": dict(current.data),
        }
        with approval_mod._lock:
            approval_mod._gateway_queues[run_id] = [current]
        app = _create_runs_app(auth_adapter)
        try:
            with (
                patch.object(auth_adapter, "_check_run_auth", return_value=None),
                patch.object(auth_adapter, "_request_owns_run", return_value=True),
                patch.object(
                    auth_adapter, "_room_grant_token", return_value="scoped-grant"
                ),
            ):
                async with TestClient(TestServer(app)) as cli:
                    missing = await cli.post(
                        f"/v1/runs/{run_id}/approval",
                        json={"choice": "once"},
                    )
                    stale = await cli.post(
                        f"/v1/runs/{run_id}/approval",
                        json={"choice": "once", "request_id": "approval-A"},
                    )
                    exact = await cli.post(
                        f"/v1/runs/{run_id}/approval",
                        json={"choice": "once", "request_id": "approval-B"},
                    )
                    missing_body = await missing.json()
                    stale_body = await stale.json()
                    exact_body = await exact.json()
        finally:
            approval_mod.unregister_gateway_notify(run_id)

        assert missing.status == 400
        assert missing_body["error"]["code"] == "approval_request_required"
        assert stale.status == 409
        assert stale_body["error"]["code"] == "approval_not_pending"
        assert exact.status == 200
        assert exact_body["request_id"] == "approval-B"
        assert current.result == "once"
        assert "approval" not in auth_adapter._run_statuses[run_id]

    @pytest.mark.asyncio
    async def test_room_grant_cannot_create_session_or_permanent_approval_policy(
        self, auth_adapter
    ):
        app = _create_runs_app(auth_adapter)
        with (
            patch.object(auth_adapter, "_check_run_auth", return_value=None),
            patch.object(auth_adapter, "_request_owns_run", return_value=True),
            patch.object(
                auth_adapter,
                "_durable_run_status",
                return_value={"status": "waiting_for_approval"},
            ),
            patch.object(
                auth_adapter, "_room_grant_token", return_value="scoped-grant"
            ),
        ):
            async with TestClient(TestServer(app)) as cli:
                permanent = await cli.post(
                    "/v1/runs/run-room/approval",
                    json={"choice": "always"},
                )
                resolve_all = await cli.post(
                    "/v1/runs/run-room/approval",
                    json={"choice": "once", "resolve_all": True},
                )
                permanent_body = await permanent.json()
                resolve_all_body = await resolve_all.json()

        assert permanent.status == 400
        assert permanent_body["error"]["code"] == "invalid_approval_choice"
        assert resolve_all.status == 400
        assert resolve_all_body["error"]["code"] == "invalid_approval_scope"

    @pytest.mark.asyncio
    async def test_invitation_uses_validated_app_managed_local_catalog(
        self, auth_adapter, monkeypatch
    ):
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv(
            "HERMES_ROOM_LINK_URL", "https://peer.example.test/hermes"
        )
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            invitation = await cli.post(
                "/v1/room-members/invitations",
                json={
                    "room_id": "room-1",
                    "home_install_id": "install-home",
                    "authority_gateway_id": "gateway-home",
                    "authority_epoch": 1,
                    "member_id": "member-reviewer",
                },
                headers={"Authorization": "Bearer sk-secret"},
            )
            body = await invitation.json()
        assert invitation.status == 201
        assert body["catalog"]["persistent_process"] is False
        assert body["catalog"]["link_modes"] == ["direct"]
        assert body["catalog"]["endpoint"] == {
            "available": True,
            "url": "https://peer.example.test/hermes",
            "transport_security": "tls",
        }
        assert body["expires_at"] == body["status_expires_at"]

    @pytest.mark.asyncio
    async def test_invitation_returns_operator_selected_status_horizon(
        self, auth_adapter
    ):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            invitation = await cli.post(
                "/v1/room-members/invitations",
                json={
                    "room_id": "room-horizon",
                    "home_install_id": "install-home",
                    "authority_gateway_id": "gateway-home",
                    "authority_epoch": 1,
                    "member_id": "member-reviewer",
                    "ttl_seconds": 600,
                    "status_ttl_seconds": 3600,
                },
                headers={"Authorization": "Bearer sk-secret"},
            )
            body = await invitation.json()

        assert invitation.status == 201
        assert body["status_expires_at"] - body["expires_at"] == 3000

    @pytest.mark.asyncio
    async def test_scoped_grant_refresh_requires_live_dispatch_authority(
        self, auth_adapter, monkeypatch
    ):
        from gateway import hosted_rooms
        from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
        from gateway.hosted_rooms import local_authority_gateway_id

        old_grant = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-old",
            room_id="room-1",
            home_install_id="install-home",
            authority_gateway_id="install-home",
            authority_epoch=1,
            member_id="member-peer",
            target_install_id=local_authority_gateway_id(),
            target_profile="default",
            issued_at=100,
            ttl_seconds=300,
            status_expires_at=1000,
        )
        old_claims = decode_room_grant(
            auth_adapter._room_grant_secret(),
            old_grant,
            permission="status",
            now=100,
        )
        hosted_rooms.reserve_peer_room(
            hosted_rooms.default_db_path(),
            claims=old_claims,
            expires_at=1000,
            now=100,
        )
        monkeypatch.setattr("gateway.platforms.api_server.time.time", lambda: 200)
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            refreshed = await cli.post(
                "/v1/room-members/grants/refresh",
                json={"ttl_seconds": 300},
                headers={"Authorization": f"HermesRoom {old_grant}"},
            )
            body = await refreshed.json()
        assert refreshed.status == 200
        assert body["grant"] != old_grant
        claims = decode_room_grant(
            auth_adapter._room_grant_secret(),
            body["grant"],
            permission="dispatch",
            now=200,
        )
        assert claims["room_id"] == "room-1"
        assert claims["home_install_id"] == "install-home"
        assert claims["status_expires_at"] == 1000

        status_only = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-status-only",
            room_id="room-1",
            home_install_id="install-home",
            authority_gateway_id="install-home",
            authority_epoch=1,
            member_id="member-peer",
            target_install_id=local_authority_gateway_id(),
            target_profile="default",
            permissions=("status",),
            issued_at=100,
            ttl_seconds=300,
            status_expires_at=1000,
        )
        status_claims = decode_room_grant(
            auth_adapter._room_grant_secret(),
            status_only,
            permission="status",
            now=100,
        )
        hosted_rooms.reserve_peer_room(
            hosted_rooms.default_db_path(),
            claims=status_claims,
            expires_at=1000,
            now=100,
        )
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            status_refresh = await cli.post(
                "/v1/room-members/grants/refresh",
                json={"ttl_seconds": 300},
                headers={"Authorization": f"HermesRoom {status_only}"},
            )
            status_refresh_body = await status_refresh.json()
        assert status_refresh.status == 401
        assert status_refresh_body["error"]["code"] == "invalid_room_grant"

        fully_expired = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-expired",
            room_id="room-1",
            home_install_id="install-home",
            authority_gateway_id="install-home",
            authority_epoch=1,
            member_id="member-peer",
            target_install_id=local_authority_gateway_id(),
            target_profile="default",
            issued_at=100,
            ttl_seconds=10,
            status_expires_at=150,
        )
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            denied = await cli.post(
                "/v1/room-members/grants/refresh",
                json={},
                headers={"Authorization": f"HermesRoom {fully_expired}"},
            )
            denied_body = await denied.json()
        assert denied.status == 401
        assert denied_body["error"]["code"] == "invalid_room_grant"

    @pytest.mark.asyncio
    async def test_scoped_grant_refresh_refuses_execution_policy_drift(
        self, auth_adapter, monkeypatch
    ):
        """Renewal must pause for reauthorization when the target's execution
        policy changed since the grant was issued — never silently mint a
        grant against the drifted policy (blocker 2, #97681 review)."""
        from gateway import hosted_rooms
        from gateway.hosted_room_peer import issue_room_grant, decode_room_grant
        from gateway.hosted_rooms import local_authority_gateway_id

        stale_digest = "c" * 64
        drifted = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-drifted",
            room_id="room-1",
            home_install_id="install-home",
            authority_gateway_id="install-home",
            authority_epoch=1,
            member_id="member-peer",
            target_install_id=local_authority_gateway_id(),
            target_profile="default",
            execution_policy_digest=stale_digest,
            issued_at=100,
            ttl_seconds=300,
            status_expires_at=1000,
        )
        drifted_claims = decode_room_grant(
            auth_adapter._room_grant_secret(),
            drifted,
            permission="status",
            now=100,
        )
        hosted_rooms.reserve_peer_room(
            hosted_rooms.default_db_path(),
            claims=drifted_claims,
            expires_at=1000,
            now=100,
        )
        monkeypatch.setattr("gateway.platforms.api_server.time.time", lambda: 200)
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            refused = await cli.post(
                "/v1/room-members/grants/refresh",
                json={"ttl_seconds": 300},
                headers={"Authorization": f"HermesRoom {drifted}"},
            )
            refused_body = await refused.json()
        assert refused.status == 403
        assert refused_body["error"]["code"] == "room_reauthorization_required"

    @pytest.mark.asyncio
    async def test_scoped_grant_refresh_fails_after_secret_rotation(
        self, auth_adapter, monkeypatch
    ):
        from gateway.hosted_room_peer import issue_room_grant
        from gateway.hosted_rooms import local_authority_gateway_id

        monkeypatch.setattr("gateway.platforms.api_server.time.time", lambda: 200)
        revoked = issue_room_grant(
            b"x" * 32,
            grant_id="grant-revoked",
            room_id="room-1",
            home_install_id="install-home",
            authority_gateway_id="install-home",
            authority_epoch=1,
            member_id="member-peer",
            target_install_id=local_authority_gateway_id(),
            target_profile="default",
            issued_at=100,
            ttl_seconds=300,
            status_expires_at=1000,
        )
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            denied = await cli.post(
                "/v1/room-members/grants/refresh",
                json={},
                headers={"Authorization": f"HermesRoom {revoked}"},
            )
            denied_body = await denied.json()
        assert denied.status == 401
        assert denied_body["error"]["code"] == "invalid_room_grant"

    def test_grant_refresh_keeps_idempotency_scope_but_member_change_does_not(
        self, auth_adapter
    ):
        from types import SimpleNamespace

        from gateway import hosted_rooms
        from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
        from gateway.hosted_rooms import local_authority_gateway_id

        common = {
            "room_id": "room-1",
            "home_install_id": "install-home",
            "authority_gateway_id": "gateway-home",
            "authority_epoch": 1,
            "member_id": "member-reviewer",
            "target_install_id": local_authority_gateway_id(),
            "target_profile": "default",
        }
        first = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-first",
            **common,
        )
        refreshed = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-refreshed",
            **common,
        )
        other_member = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-other-member",
            **{**common, "member_id": "member-other"},
        )
        for grant in (first, other_member):
            claims = decode_room_grant(
                auth_adapter._room_grant_secret(),
                grant,
                permission="status",
            )
            hosted_rooms.reserve_peer_room(
                hosted_rooms.default_db_path(),
                claims=claims,
                expires_at=float(claims["status_expires_at"]),
            )

        def request(token):
            return SimpleNamespace(
                headers={"Authorization": f"HermesRoom {token}"},
                method="POST",
                path="/v1/runs",
            )

        first_scope = auth_adapter._run_idempotency_scope(request(first))
        assert auth_adapter._run_idempotency_scope(request(refreshed)) == first_scope
        assert auth_adapter._run_idempotency_scope(request(other_member)) != first_scope

    @pytest.mark.asyncio
    async def test_scoped_grant_revoke_is_idempotent_and_fences_prior_lineage(
        self, auth_adapter, monkeypatch
    ):
        from gateway import hosted_rooms
        from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
        from gateway.hosted_rooms import local_authority_gateway_id

        for target in (
            "gateway.platforms.api_server.time.time",
            "gateway.hosted_rooms_common.time.time",
        ):
            monkeypatch.setattr(target, lambda: 200)
        claims = {
            "room_id": "room-1",
            "home_install_id": "install-home",
            "authority_gateway_id": "install-home",
            "authority_epoch": 1,
            "member_id": "member-peer",
            "target_install_id": local_authority_gateway_id(),
            "target_profile": "default",
        }
        old_grant = issue_room_grant(
            auth_adapter._room_grant_secret(),
            grant_id="grant-old",
            **claims,
            issued_at=100,
            ttl_seconds=300,
            status_expires_at=1000,
        )
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            first = await cli.post(
                "/v1/room-members/grants/revoke",
                json={},
                headers={"Authorization": f"HermesRoom {old_grant}"},
            )
            repeated = await cli.post(
                "/v1/room-members/grants/revoke",
                json={},
                headers={"Authorization": f"HermesRoom {old_grant}"},
            )
            denied = await cli.get(
                "/v1/room-members/capabilities",
                headers={"Authorization": f"HermesRoom {old_grant}"},
            )
            denied_run = await cli.post(
                "/v1/runs",
                data="{never parsed",
                headers={
                    "Authorization": f"HermesRoom {old_grant}",
                    "Content-Type": "application/json",
                },
            )
            denied_body = await denied.json()
            denied_run_body = await denied_run.json()
            future_grant = issue_room_grant(
                auth_adapter._room_grant_secret(),
                grant_id="grant-repaired",
                **claims,
                issued_at=201,
                ttl_seconds=300,
                status_expires_at=1000,
            )
            future_claims = decode_room_grant(
                auth_adapter._room_grant_secret(),
                future_grant,
                permission="status",
                now=201,
            )
            hosted_rooms.reserve_peer_room(
                hosted_rooms.default_db_path(),
                claims=future_claims,
                expires_at=1000,
                now=201,
            )
            repaired = await cli.get(
                "/v1/room-members/capabilities",
                headers={"Authorization": f"HermesRoom {future_grant}"},
            )
        assert first.status == repeated.status == 200
        assert denied.status == 403
        assert denied_body["error"]["code"] == "room_reauthorization_required"
        assert denied_run.status == 403
        assert (
            denied_run_body["error"]["code"]
            == "room_reauthorization_required"
        )
        assert auth_adapter._pending_agent_requests == 0
        assert repaired.status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "suffix"),
        [("GET", ""), ("POST", "/stop")],
    )
    async def test_room_grant_cannot_access_ownerless_compat_run(
        self, auth_adapter, tmp_path, method, suffix
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            invitation = await cli.post(
                "/v1/room-members/invitations",
                json={
                    "room_id": "room-1",
                    "home_install_id": "install-home",
                    "authority_gateway_id": "gateway-home",
                    "authority_epoch": 1,
                    "member_id": "member-reviewer",
                },
                headers={"Authorization": "Bearer sk-secret"},
            )
            grant = (await invitation.json())["grant"]
            adapter._run_statuses["run_ownerless"] = {
                "run_id": "run_ownerless",
                "status": "running",
            }
            response = await cli.request(
                method,
                f"/v1/runs/run_ownerless{suffix}",
                json={} if method == "POST" else None,
                headers={"Authorization": f"HermesRoom {grant}"},
            )
        assert response.status == 404

    @pytest.mark.asyncio
    async def test_scoped_grant_admits_group_session_run_without_peer_api_key(
        self, auth_adapter, tmp_path
    ):
        from gateway import hosted_rooms

        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            invitation = await cli.post(
                "/v1/room-members/invitations",
                json={
                    "grant_id": "grant-room-1",
                    "room_id": "room-1",
                    "home_install_id": "install-home",
                    "authority_gateway_id": "gateway-home",
                    "authority_epoch": 1,
                    "member_id": "member-reviewer",
                    "ttl_seconds": 3600,
                },
                headers={"Authorization": "Bearer sk-secret"},
            )
            invitation_body = await invitation.json()
            assert invitation.status == 201
            grant = invitation_body["grant"]
            catalog = invitation_body["catalog"]
            probe = await cli.get(
                "/v1/room-members/capabilities",
                headers={"Authorization": f"HermesRoom {grant}"},
            )
            probe_body = await probe.json()
            assert probe.status == 200
            assert probe_body["catalog"] == catalog
            prompt = "Review this room message."
            dispatch = {
                "protocol_version": 2,
                "room_id": "room-1",
                "home_install_id": "install-home",
                "authority_gateway_id": "gateway-home",
                "authority_epoch": 1,
                "member_id": "member-reviewer",
                "target_install_id": catalog["installation_id"],
                "target_profile": "default",
                "task_id": "task-room-1",
                "execution_generation": 1,
                "source_event_seq": 1,
                "cancellation_scope_id": "cancel-room-1",
                "prompt": prompt,
                "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                "capability_digest": catalog["catalog_digest"],
                "execution_policy_digest": catalog["execution_policy"][
                    "policy_digest"
                ],
                "trace_id": "trace-room-1",
            }
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {
                    "final_response": "Scoped room reply."
                }
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                started = await cli.post(
                    "/v1/runs",
                    json={"input": prompt, "hosted_room_dispatch": dispatch},
                    headers={
                        "Authorization": f"HermesRoom {grant}",
                        "Idempotency-Key": "room:task-room-1:1",
                    },
                )
                started_body = await started.json()
                assert started.status == 202
                run_id = started_body["run_id"]
                for _ in range(40):
                    status = await cli.get(
                        f"/v1/runs/{run_id}",
                        headers={"Authorization": f"HermesRoom {grant}"},
                    )
                    status_body = await status.json()
                    if status_body.get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)
            assert status.status == 200
            assert status_body["output"] == "Scoped room reply."
            session_id = status_body["session_id"]
            db = await adapter._ensure_session_db_async()
            row = db.get_session(session_id)
            assert row["source"] == "bot_room"
            assert row["title"] == "Group: room-1"
            assert catalog["installation_id"] == (
                hosted_rooms.local_authority_gateway_id()
            )

    @pytest.mark.asyncio
    async def test_scoped_grant_rejects_capability_and_target_tampering(
        self, auth_adapter, tmp_path
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            invitation = await cli.post(
                "/v1/room-members/invitations",
                json={
                    "room_id": "room-1",
                    "home_install_id": "install-home",
                    "authority_gateway_id": "gateway-home",
                    "authority_epoch": 1,
                    "member_id": "member-reviewer",
                },
                headers={"Authorization": "Bearer sk-secret"},
            )
            invitation_body = await invitation.json()
            prompt = "Review."
            dispatch = {
                "protocol_version": 2,
                "room_id": "room-1",
                "home_install_id": "install-home",
                "authority_gateway_id": "gateway-home",
                "authority_epoch": 1,
                "member_id": "member-reviewer",
                "target_install_id": invitation_body["catalog"]["installation_id"],
                "target_profile": "default",
                "task_id": "task-room-1",
                "execution_generation": 1,
                "source_event_seq": 1,
                "cancellation_scope_id": "cancel-room-1",
                "prompt": prompt,
                "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                "capability_digest": "f" * 64,
                "execution_policy_digest": invitation_body["catalog"][
                    "execution_policy"
                ]["policy_digest"],
                "trace_id": "trace-room-1",
            }
            with patch.object(adapter, "_create_agent") as create:
                rejected = await cli.post(
                    "/v1/runs",
                    json={"input": prompt, "hosted_room_dispatch": dispatch},
                    headers={
                        "Authorization": f"HermesRoom {invitation_body['grant']}",
                        "Idempotency-Key": "room:task-room-1:1",
                    },
                )
            assert rejected.status == 403
            create.assert_not_called()
