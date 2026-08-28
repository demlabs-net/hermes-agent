from types import SimpleNamespace

from agent.terminal_tool_guard import (
    build_required_terminal_tool_nudge,
    forced_terminal_tool_after_repeated_searches,
    terminal_tool_request_scope,
)


ORDER = "Task task_abc123 from manager:\nBuild the page."
REPORT = "mcp__swarm__report"


def agent(**overrides):
    values = {
        "_required_terminal_tools": [REPORT],
        "_required_terminal_tool_platforms": ["api_server"],
        "_required_terminal_tool_user_pattern": r"^Task task_",
        "_required_terminal_tool_result_pattern": "",
        "_required_terminal_tool_max_nudges": 4,
        "_required_terminal_tool_force_after_searches": 2,
        "_required_terminal_tool_nudge": "",
        "platform": "api_server",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def nudge(messages, **overrides):
    return build_required_terminal_tool_nudge(
        agent=agent(**overrides),
        user_message=ORDER,
        messages=messages,
        attempts=0,
        current_turn_user_idx=0,
    )


def test_missing_required_result_nudges_even_when_lazy_hidden():
    assert REPORT in nudge([{"role": "user", "content": ORDER}])


def test_successful_result_satisfies_contract_but_failure_does_not():
    base = [{"role": "user", "content": ORDER}]
    assert nudge(base + [{"role": "tool", "name": REPORT, "content": '{"success":true}'}]) is None
    assert REPORT in nudge(
        base + [{"role": "tool", "name": REPORT, "content": '{"success":false}'}]
    )


def test_result_pattern_rejects_progress_and_accepts_terminal_status():
    pattern = r'"status"\s*:\s*"(?:completed|blocked|failed)"'
    base = [{"role": "user", "content": ORDER}]
    in_progress = {
        "role": "tool",
        "name": REPORT,
        "content": '{"result":{"ok":true,"status":"in_progress"}}',
    }
    completed = {
        "role": "tool",
        "name": REPORT,
        "content": '{"result":{"ok":true,"status":"completed"}}',
    }

    assert REPORT in nudge(
        base + [in_progress],
        _required_terminal_tool_result_pattern=pattern,
    )
    assert nudge(
        base + [in_progress, completed],
        _required_terminal_tool_result_pattern=pattern,
    ) is None


def test_result_pattern_reads_structured_content_and_invalid_regex_fails_closed():
    base = [{"role": "user", "content": ORDER}]
    terminal = {
        "role": "tool",
        "name": REPORT,
        "content": {"structuredContent": {"status": "blocked"}},
    }
    pattern = r'"status"\s*:\s*"(?:completed|blocked|failed)"'

    assert nudge(
        base + [terminal],
        _required_terminal_tool_result_pattern=pattern,
    ) is None
    assert REPORT in nudge(
        base + [terminal],
        _required_terminal_tool_result_pattern="(",
    )


def test_platform_and_attempt_limits_disable_nudge():
    messages = [{"role": "user", "content": ORDER}]
    assert nudge(messages, platform="telegram") is None
    assert build_required_terminal_tool_nudge(
        agent=agent(),
        user_message=ORDER,
        messages=messages,
        attempts=4,
    ) is None


def test_repeated_exact_search_for_required_tool_forces_it():
    search_call = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "tool_search"}}],
    }
    search_result = {
        "role": "tool",
        "name": "tool_search",
        "content": '{"query":"mcp__swarm__report"}',
    }
    messages = [
        {"role": "user", "content": ORDER},
        search_call,
        search_result,
        search_call,
        search_result,
    ]
    assert forced_terminal_tool_after_repeated_searches(
        agent=agent(), user_message=ORDER, messages=messages
    ) == REPORT


def test_forced_request_scope_preserves_historical_function_names():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "terminal"}},
                {"function": {"name": "tool_search"}},
            ],
        }
    ]
    assert terminal_tool_request_scope(
        messages=messages, required_tool=REPORT
    ) == frozenset({REPORT, "terminal", "tool_search"})
