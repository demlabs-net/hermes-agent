"""Bound the extra request issued after primary transport recovery."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


class ReadTimeout(Exception):
    pass


def _agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key-abcdef12",
            base_url="https://example.invalid/v1",
            provider="custom",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._api_max_retries = 2
    return agent


def test_primary_transport_recovery_adds_only_one_physical_attempt() -> None:
    agent = _agent()
    calls = 0

    def fail_every_call(_api_kwargs):
        nonlocal calls
        calls += 1
        raise ReadTimeout("provider sent no response headers")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fail_every_call),
        patch.object(agent, "_try_recover_primary_transport", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert calls == 3  # two configured attempts plus one rebuilt-client probe
