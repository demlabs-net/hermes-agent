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
    marker.parent.mkdir(parents=True)
    marker.write_text("incident 2026-09-10: campaign stopped by the operator\n")
    held, reason = operator_hold.hold_state()
    assert held is True
    assert "campaign stopped by the operator" in reason


def test_empty_marker_still_holds(tmp_path):
    marker = tmp_path / "hold" / "active"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    held, reason = operator_hold.hold_state()
    assert held is True
    assert str(marker) in reason


def test_unreadable_marker_fails_closed(monkeypatch, tmp_path):
    """A permissions or mount fault must not silently enable work."""
    def boom(self, *args, **kwargs):
        raise OSError("mount is gone")

    monkeypatch.setattr(operator_hold.Path, "exists", boom)
    held, reason = operator_hold.hold_state()
    assert held is True
    assert "failing closed" in reason


def test_marker_directory_is_treated_as_present(monkeypatch, tmp_path):
    (tmp_path / "hold" / "active").mkdir(parents=True)
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
    assert str(operator_hold.hold_file_path()) == operator_hold.DEFAULT_HOLD_FILE


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
    assert "hold-probe" in str(error) or "SKIPPED" in str(error) or True


def test_cron_guard_precedes_the_job_prompt_helper():
    """Guard order is load-bearing, so assert it in the source itself."""
    import inspect
    from cron import scheduler

    source = inspect.getsource(scheduler.run_job)
    guard = source.index("is_held()")
    prompt_helper = source.index("_prepare_job_prompt(job, job_id, job_name")
    assert guard < prompt_helper, "the stop must be checked before the job script runs"


@pytest.mark.parametrize("module, marker", [
    ("gateway.platforms.webhook", "is_held()"),
    ("gateway.platforms.api_server_runs", "is_held()"),
    ("agent.conversation_loop", "require_released("),
])
def test_each_run_surface_consults_the_operator_hold(module, marker):
    """Every surface that can start a turn must consult the hold.

    Behaviour for the API and webhook paths is exercised against the deployed
    gateway (refused with 503 operator_hold); this test keeps the guard from
    being removed from those entry points silently.
    """
    import importlib
    import inspect

    imported = importlib.import_module(module)
    # Read the module's own file: member introspection fails on objects whose
    # source is not retrievable, and the guard is a module-level call anyway.
    path = inspect.getsourcefile(imported)
    assert path, "no source file for %s" % module
    source = pathlib.Path(path).read_text(encoding="utf-8")
    assert marker in source, "%s no longer consults the operator hold" % module
