"""Operator stop control: authority is external, and it fails closed.

The swarm and SLC holds cannot constrain a cron fire, an API-server run or a
platform webhook, because those start a conversation directly. This module is
the chokepoint they consult, so its behaviour is load-bearing: a false negative
means work runs after an operator stop.
"""
import pathlib

import pytest

from agent import operator_hold


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_OPERATOR_HOLD", raising=False)
    monkeypatch.setenv("HERMES_OPERATOR_HOLD_FILE", str(tmp_path / "hold" / "active"))
    monkeypatch.setattr(operator_hold, "_POLICY", operator_hold.OperatorPolicy(tmp_path / "hold" / "active", True, ""))
    (tmp_path / "hold").mkdir()
    (tmp_path / "hold" / "control.json").write_text('{"generation":"initial","held":false}')
    yield


def test_released_by_default(tmp_path):
    held, reason = operator_hold.hold_state()
    assert held is False
    assert reason == ""
    assert operator_hold.is_held() is False
    operator_hold.require_released("cron job")  # must not raise


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_env_flag_holds(monkeypatch, value):
    monkeypatch.setenv("HERMES_OPERATOR_HOLD", value)
    held, reason = operator_hold.hold_state()
    assert held is True
    assert "HERMES_OPERATOR_HOLD" in reason


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "disabled"])
def test_non_truthy_env_does_not_hold(monkeypatch, value):
    monkeypatch.setenv("HERMES_OPERATOR_HOLD", value)
    assert operator_hold.is_held() is False


def test_marker_file_holds_and_carries_the_reason(tmp_path):
    marker = tmp_path / "hold" / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("incident 2026-09-10: campaign stopped by the operator\n")
    held, reason = operator_hold.hold_state()
    assert held is True
    assert "campaign stopped by the operator" in reason


def test_empty_marker_still_holds(tmp_path):
    marker = tmp_path / "hold" / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    held, reason = operator_hold.hold_state()
    assert held is True
    assert str(marker) in reason


def test_unreadable_marker_fails_closed(monkeypatch, tmp_path):
    """A permissions or mount fault must not silently enable work."""
    def boom(self, *args, **kwargs):
        raise OSError("mount is gone")

    monkeypatch.setattr(operator_hold.Path, "lstat", boom)
    held, reason = operator_hold.hold_state()
    assert held is True
    assert "failing closed" in reason


def test_marker_directory_is_treated_as_present(monkeypatch, tmp_path):
    (tmp_path / "hold" / "active").mkdir(parents=True, exist_ok=True)
    held, reason = operator_hold.hold_state()
    assert held is True


def test_require_released_raises_with_marker_tag(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_HOLD", "true")
    with pytest.raises(operator_hold.OperatorHoldError) as excinfo:
        operator_hold.require_released("conversation turn")
    message = str(excinfo.value)
    assert operator_hold.HOLD_MARKER in message
    assert "conversation turn" in message
    # The remediation must not suggest that a queued or historical order releases it.
    assert "queued or historical instructions do not release it" in message


def test_hold_message_is_empty_when_released():
    assert operator_hold.hold_message() == ""


def test_configured_default_path_is_used_when_env_absent(monkeypatch):
    monkeypatch.delenv("HERMES_OPERATOR_HOLD_FILE", raising=False)
    # Removing mutable env does not change the pinned operator path.
    assert operator_hold.hold_file_path() == operator_hold._POLICY.marker


def test_conversation_entry_is_guarded(monkeypatch):
    """The chokepoint every envelope passes through must refuse while held.

    A dummy agent is enough: the guard runs before the agent is touched, so an
    unguarded entry point would fail differently and this test would catch it.
    """
    from agent.conversation_loop import run_conversation

    monkeypatch.setenv("HERMES_OPERATOR_HOLD", "true")
    with pytest.raises(operator_hold.OperatorHoldError):
        run_conversation(None, "hello")


def test_conversation_entry_allows_when_released(monkeypatch):
    """With no hold the guard must not interfere: a bad agent still fails its own way."""
    from agent.conversation_loop import run_conversation

    monkeypatch.delenv("HERMES_OPERATOR_HOLD", raising=False)
    with pytest.raises(Exception) as excinfo:
        run_conversation(None, "hello")
    assert not isinstance(excinfo.value, operator_hold.OperatorHoldError)


def test_cron_run_job_skips_before_running_any_job_script(monkeypatch):
    """The cron stop must fire before the job's own wake-gate script runs.

    That ordering is the point: a held job must not execute job-defined shell
    merely because the model turn is skipped afterwards.
    """
    from cron.scheduler import run_job

    monkeypatch.setenv("HERMES_OPERATOR_HOLD", "true")
    success, output, response, error = run_job(
        {"id": "probe", "name": "hold-probe", "prompt": "must not run"})
    assert success is False
    assert output == "" and response == ""
    assert operator_hold.HOLD_MARKER in (error or "")


def rotate(tmp_path, generation, held=False):
    import json
    path = tmp_path / "hold" / "control.json"
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps({"generation": generation, "held": held}))
    staging.replace(path)


def test_missing_control_fails_closed(tmp_path):
    (tmp_path / "hold" / "control.json").unlink()
    with pytest.raises(operator_hold.OperatorHoldError):
        with operator_hold.run_scope():
            pytest.fail("admitted missing control")


@pytest.mark.parametrize("raw", ["broken", "[]", '{"generation":"x","held":"false"}', '{"held":false}'])
def test_bad_control_fails_closed(tmp_path, raw):
    (tmp_path / "hold" / "control.json").write_text(raw)
    assert operator_hold.is_held()


def test_generation_rotation_without_observing_hold_revokes_old_run(tmp_path):
    called = []
    with operator_hold.run_scope():
        rotate(tmp_path, "held", True)
        rotate(tmp_path, "released")
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.guarded_call(called.append, "unsafe")
        with pytest.raises(operator_hold.OperatorHoldError):
            with operator_hold.run_scope():
                pytest.fail("nested run renewed grant")
    with operator_hold.run_scope():
        operator_hold.guarded_call(called.append, "new run")
    assert called == ["new run"]


def test_marker_observation_latches_revocation(tmp_path):
    marker = tmp_path / "hold" / "active"
    with operator_hold.run_scope():
        marker.touch()
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_released()
        marker.unlink()
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_released()


def test_real_parallel_queue_inherits_old_generation(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tools.thread_context import propagate_context_to_thread
    called = []
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool, operator_hold.run_scope():
        first = pool.submit(blocker)
        assert entered.wait(5)
        queued = pool.submit(propagate_context_to_thread(lambda: operator_hold.guarded_call(called.append, "unsafe")))
        rotate(tmp_path, "held", True)
        rotate(tmp_path, "released")
        release.set()
        first.result(5)
        with pytest.raises(operator_hold.OperatorHoldError):
            queued.result(5)
    assert called == []


def test_cron_never_prepares_script_when_held(monkeypatch):
    from cron import scheduler
    called = []
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *a: called.append(a))
    monkeypatch.setenv("HERMES_OPERATOR_HOLD", "true")
    for extra in ({"script": "must-not-execute", "no_agent": True}, {}):
        result = scheduler.run_job({"id": "probe", "prompt": "hello", **extra})
        assert result[0] is False
        assert "operator_hold" in result[3]
    assert called == []


def test_active_conversation_cannot_dispatch_after_release(monkeypatch, tmp_path):
    from agent import conversation_loop
    invoked = []
    def turn(*args, **kwargs):
        rotate(tmp_path, "held", True)
        rotate(tmp_path, "new")
        operator_hold.guarded_call(invoked.append, "tool")
    monkeypatch.setattr(conversation_loop, "_run_conversation_turn", turn)
    with pytest.raises(operator_hold.OperatorHoldError):
        conversation_loop.run_conversation(None, "hello")
    assert invoked == []


@pytest.mark.asyncio
async def test_webhook_denial_does_not_reserve_and_same_delivery_retries(monkeypatch, tmp_path):
    from tests.gateway.test_webhook_adapter import _make_adapter, _mock_request, _INSECURE_NO_AUTH
    from unittest.mock import AsyncMock
    adapter = _make_adapter(routes={"probe": {"prompt": "hello", "secret": _INSECURE_NO_AUTH}})
    adapter.handle_message = AsyncMock()
    request = _mock_request(headers={"X-GitHub-Delivery": "same", "X-Hermes-Operator-Generation": "initial"},
                            body=b"{}", match_info={"route_name": "probe"})
    rotate(tmp_path, "held", True)
    assert (await adapter._handle_webhook(request)).status == 503
    assert adapter._seen_deliveries == {}
    assert adapter._delivery_info == {}
    rotate(tmp_path, "released")
    assert (await adapter._handle_webhook(request)).status == 503
    assert adapter._seen_deliveries == {}
    request.headers["X-Hermes-Operator-Generation"] = "released"
    assert (await adapter._handle_webhook(request)).status == 202
    import asyncio
    await asyncio.gather(*adapter._background_tasks)
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "stale"])
async def test_api_denial_before_body_or_reservation(header):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from gateway.platforms import api_server_runs, api_server
    request = SimpleNamespace(headers={} if header is None else {"X-Hermes-Operator-Generation": header}, json=AsyncMock())
    adapter = Mock()
    response = await api_server_runs._handle_runs(adapter, request, _api_server=api_server)
    assert response.status == 503
    request.json.assert_not_awaited()
    assert adapter.mock_calls == []


def test_real_registry_handler_not_invoked_after_revocation(tmp_path):
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    called = []
    registry.register(name="hold_probe", toolset="test", schema={"name": "hold_probe"},
                      handler=lambda args, **kw: called.append(args) or "{}")
    with operator_hold.run_scope():
        rotate(tmp_path, "new")
        result = registry.dispatch("hold_probe", {})
    assert "operator_hold" in result
    assert called == []


def test_cron_stale_generation_never_prepares_prompt(monkeypatch):
    from cron import scheduler
    called = []
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *a: called.append(a))
    for generation in (None, "stale"):
        result = scheduler.run_job({"id": "old", "prompt": "hello", "operator_generation": generation})
        assert result[0] is False
        assert "generation" in result[3]
    assert called == []


def test_ro_cron_authorization_binds_payload_and_rejects_scripts(tmp_path):
    import json
    job = {"id": "one", "prompt": "approved", "operator_generation": "initial"}
    control = tmp_path / "hold" / "control.json"
    data = json.loads(control.read_text())
    data["authorized_cron_jobs"] = {"one": operator_hold.cron_payload_digest(job)}
    control.write_text(json.dumps(data))
    with operator_hold.run_scope():
        operator_hold.require_cron_authorization(job)
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_cron_authorization({**job, "prompt": "different"})
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_cron_authorization({**job, "script": "/tmp/mutable.sh"})


def test_operator_cli_rotates_durable_control(tmp_path):
    import json, subprocess, sys
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "operator_hold.py"
    directory = tmp_path / "hold"
    before = json.loads((directory / "control.json").read_text())["generation"]
    subprocess.run([sys.executable, str(script), str(directory), "hold"], check=True, capture_output=True)
    held = json.loads((directory / "control.json").read_text())
    assert held["held"] is True and held["generation"] != before
    (directory / "active").touch()
    subprocess.run([sys.executable, str(script), str(directory), "release"], check=True, capture_output=True)
    released = json.loads((directory / "control.json").read_text())
    assert released["held"] is False and released["generation"] != held["generation"]
    assert not (directory / "active").exists()
    assert released["authorized_cron_jobs"] == {}


def test_operator_cli_authorizes_one_exact_cron_job(tmp_path):
    """The CLI must bind ONE exact job id to its payload digest without rewriting
    the payload, and leave every other job unauthorized."""
    import json, subprocess, sys
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "operator_hold.py"
    directory = tmp_path / "hold"
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    job = {"id": "digest-job", "prompt": "approved payload", "schedule": {"expr": "0 9 * * *"}}
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))

    subprocess.run([sys.executable, str(script), str(directory), "authorize-cron",
                    "--job-id", "digest-job", "--hermes-home", str(home)],
                   check=True, capture_output=True)

    control = json.loads((directory / "control.json").read_text())
    stored = json.loads((home / "cron" / "jobs.json").read_text())["jobs"][0]
    assert control["authorized_cron_jobs"] == {"digest-job": operator_hold.cron_payload_digest(stored)}
    assert stored["operator_generation"] == control["generation"]
    assert stored["prompt"] == "approved payload"


def test_operator_cli_refuses_unknown_job_and_payload_edit(tmp_path):
    """An unknown id is refused without recording anything, and editing the payload
    afterwards invalidates the recorded digest."""
    import json, subprocess, sys
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "operator_hold.py"
    directory = tmp_path / "hold"
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    job = {"id": "digest-job", "prompt": "approved payload"}
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))

    unknown = subprocess.run([sys.executable, str(script), str(directory), "authorize-cron",
                              "--job-id", "not-a-job", "--hermes-home", str(home)],
                             capture_output=True)
    assert unknown.returncode != 0
    assert "authorized_cron_jobs" not in json.loads((directory / "control.json").read_text())

    subprocess.run([sys.executable, str(script), str(directory), "authorize-cron",
                    "--job-id", "digest-job", "--hermes-home", str(home)],
                   check=True, capture_output=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "digest-job", "prompt": "edited payload"}]}))
    edited = json.loads((home / "cron" / "jobs.json").read_text())["jobs"][0]
    with operator_hold.run_scope():
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_cron_authorization(edited)


def test_provider_dispatch_does_not_invoke_client_after_rotation(tmp_path):
    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
    from types import SimpleNamespace
    called = []
    with operator_hold.run_scope():
        rotate(tmp_path, "released-again")
        with pytest.raises(operator_hold.OperatorHoldError):
            _dispatch_nonstreaming_api_request(SimpleNamespace(api_mode="chat_completions"), {}, make_client=lambda *a: called.append(a))
    assert called == []


def test_mutable_env_cannot_downgrade_operator_policy(monkeypatch, tmp_path):
    original = operator_hold.hold_file_path()
    monkeypatch.delenv("HERMES_OPERATOR_HOLD_FILE")
    assert operator_hold.configured_policy()
    assert operator_hold.hold_file_path() == original
    with operator_hold.run_scope():
        with pytest.raises(operator_hold.OperatorHoldError):
            operator_hold.require_generation(None)
    monkeypatch.setenv("HERMES_OPERATOR_HOLD_FILE", str(tmp_path / "fake"))
    (tmp_path / "hold" / "control.json").unlink()
    with pytest.raises(operator_hold.OperatorHoldError):
        with operator_hold.run_scope():
            pytest.fail("configured control downgraded")


def test_startup_env_hold_cannot_be_cleared(monkeypatch, tmp_path):
    monkeypatch.setattr(operator_hold, "_POLICY", operator_hold.OperatorPolicy(tmp_path / "hold" / "active", True, "true"))
    monkeypatch.delenv("HERMES_OPERATOR_HOLD", raising=False)
    assert operator_hold.is_held()


def test_cron_extra_prompt_rejected_before_helper(monkeypatch, tmp_path):
    import json
    from cron import scheduler
    job = {"id": "one", "prompt": "approved", "operator_generation": "initial"}
    control = tmp_path / "hold" / "control.json"
    data = json.loads(control.read_text())
    data["authorized_cron_jobs"] = {"one": operator_hold.cron_payload_digest(job)}
    control.write_text(json.dumps(data))
    called = []
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *a: called.append(a))
    result = scheduler.run_job(job, extra_prompt="unapproved external task")
    assert result[0] is False and "extra_prompt" in result[3]
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat_completions", "responses"])
async def test_openai_actual_queued_worker_retains_revoked_grant(tmp_path, surface):
    import asyncio, threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    from gateway.platforms.api_server import APIServerAdapter
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    previous = loop._default_executor
    loop.set_default_executor(pool)
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5)
    first = pool.submit(blocker)
    assert entered.wait(5)
    adapter = Mock()
    adapter._inflight_agent_runs = 0
    try:
        with operator_hold.run_scope():
            # Both OpenAI routes call this real executor method. Queue the request
            # behind an occupied worker, rotate without the request polling held.
            task = asyncio.create_task(APIServerAdapter._run_agent(adapter, "hello", [], session_id=surface))
            await asyncio.sleep(0)
            rotate(tmp_path, "held", True)
            rotate(tmp_path, "new")
            release.set()
            with pytest.raises(operator_hold.OperatorHoldError):
                await task
        adapter._create_agent.assert_not_called()
        assert adapter._inflight_agent_runs == 0
    finally:
        release.set()
        first.result(5)
        pool.shutdown()
        loop._default_executor = previous


def test_process_bootstrap_pins_policy_before_env_mutation(tmp_path):
    import os, subprocess, sys
    env = dict(os.environ, HERMES_OPERATOR_HOLD_FILE=str(tmp_path / "hold" / "active"))
    code = """
import agent
import os
from agent import operator_hold as hold
original = hold.hold_file_path()
os.environ.pop('HERMES_OPERATOR_HOLD_FILE', None)
assert hold.configured_policy()
assert hold.hold_file_path() == original
with hold.run_scope():
    try:
        hold.require_generation(None)
    except hold.OperatorHoldError:
        pass
    else:
        raise AssertionError('header requirement downgraded')
(original.parent / 'control.json').unlink()
try:
    with hold.run_scope():
        raise AssertionError('missing control admitted')
except hold.OperatorHoldError:
    pass
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
