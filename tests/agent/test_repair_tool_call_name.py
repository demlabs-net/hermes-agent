"""Tests for AIAgent._repair_tool_call — tool-name normalization.

Regression guard for #14784: Claude-style models sometimes emit
class-like tool-call names (``TodoTool_tool``, ``Patch_tool``,
``BrowserClick_tool``, ``PatchTool``). Before the fix they returned
"Unknown tool" even though the target tool was registered under a
snake_case name. The repair routine now normalizes CamelCase,
strips trailing ``_tool`` / ``-tool`` / ``tool`` suffixes (up to
twice to handle double-tacked suffixes like ``TodoTool_tool``), and
falls back to fuzzy match.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


VALID = {
    "todo",
    "patch",
    "browser_click",
    "browser_navigate",
    "web_search",
    "read_file",
    "write_file",
    "terminal",
    "execute_code",
    "session_search",
}


@pytest.fixture
def repair():
    """Return a bound _repair_tool_call built on a minimal shell agent.

    We avoid constructing a real AIAgent (which pulls in credential
    resolution, session DB, etc.) because the repair routine only
    reads self.valid_tool_names. A SimpleNamespace stub is enough to
    bind the unbound function.
    """
    from run_agent import AIAgent
    stub = SimpleNamespace(valid_tool_names=VALID)
    return AIAgent._repair_tool_call.__get__(stub, AIAgent)


class TestExistingBehaviorStillWorks:
    """Pre-existing repairs must keep working (no regressions)."""

    def test_lowercase_already_matches(self, repair):
        assert repair("browser_click") == "browser_click"







class TestClassLikeEmissions:
    """Regression coverage for #14784 — CamelCase + _tool suffix variants."""

    def test_camel_case_no_suffix(self, repair):
        assert repair("BrowserClick") == "browser_click"









class TestEdgeCases:
    """Edge inputs that must not crash or produce surprising results."""

    def test_empty_string(self, repair):
        assert repair("") is None

    def test_registered_but_hidden_tool_is_not_fuzzy_mapped(self, monkeypatch):
        """A deferred read must never become a similarly named mutation."""
        from agent.agent_runtime_helpers import repair_tool_call
        from tools.registry import registry

        hidden = "mcp__bridge_regression__read_resource"
        visible = "mcp__bridge_regression__order"
        original_get_entry = registry.get_entry
        monkeypatch.setattr(
            registry,
            "get_entry",
            lambda name: object() if name == hidden else original_get_entry(name),
        )
        agent = SimpleNamespace(valid_tool_names={visible})

        assert repair_tool_call(agent, hidden) is None


class TestDeferredToolBridge:
    def test_exact_scoped_call_is_wrapped_with_original_arguments(self, monkeypatch):
        from agent.agent_runtime_helpers import bridge_deferred_tool_call
        import model_tools

        name = "mcp__swarm__read_resource"
        monkeypatch.setattr(
            model_tools,
            "get_scoped_deferred_tool_names",
            lambda **_kwargs: frozenset({name}),
        )
        agent = SimpleNamespace(
            valid_tool_names={"tool_call"},
            enabled_toolsets=["mcp-swarm"],
            disabled_toolsets=None,
        )
        call = SimpleNamespace(
            function=SimpleNamespace(
                name=name,
                arguments='{"uri":"swarm://operations"}',
            )
        )

        assert bridge_deferred_tool_call(agent, call) == name
        assert call.function.name == "tool_call"
        assert json.loads(call.function.arguments) == {
            "name": name,
            "arguments": {"uri": "swarm://operations"},
        }

    def test_out_of_scope_call_is_not_wrapped(self, monkeypatch):
        from agent.agent_runtime_helpers import bridge_deferred_tool_call
        import model_tools

        monkeypatch.setattr(
            model_tools,
            "get_scoped_deferred_tool_names",
            lambda **_kwargs: frozenset(),
        )
        agent = SimpleNamespace(
            valid_tool_names={"tool_call"},
            enabled_toolsets=["safe"],
            disabled_toolsets=None,
        )
        call = SimpleNamespace(
            function=SimpleNamespace(name="mcp__swarm__order", arguments="{}")
        )

        assert bridge_deferred_tool_call(agent, call) is None
        assert call.function.name == "mcp__swarm__order"





class TestVolcEngineXmlPollution:
    """Regression coverage for #33007 — VolcEngine ``api/plan`` endpoint
    leaks raw XML attribute fragments into ``tool_use.name``.

    Observed in production with the ``anthropic_messages`` API mode:

        terminal" parameter="command" string="true
        execute_code" parameter="code" string="true
        session_search" parameter="session_id" string="true

    The fix trims at the first ``"``/``'``/``<``/``>`` so the rest of
    the repair pipeline can resolve the cleaned name to a real tool.
    """

    def test_terminal_with_xml_attribute_pollution(self, repair):
        # Exact pattern from the bug report (terminal call).
        polluted = 'terminal" parameter="command" string="true'
        assert repair(polluted) == "terminal"




    def test_tool_name_with_trailing_quote_only(self, repair):
        # Minimal leak — just a stray trailing quote, no full attribute.
        assert repair('terminal"') == "terminal"



    def test_clean_tool_name_unaffected_by_sanitizer(self, repair):
        # Pure passthrough — no XML/quote chars, no change.
        assert repair("execute_code") == "execute_code"
        assert repair("session_search") == "session_search"

    def test_space_separated_name_still_normalizes(self, repair):
        # Critical: the XML strip must NOT consume whitespace, or the
        # legitimate ``"write file" -> write_file`` repair path breaks.
        assert repair("write file") == "write_file"


    def test_leading_quote_falls_through_to_fuzzy_match(self, repair):
        # Sanitizer only trims when the XML char is at idx > 0 — a
        # name that *starts* with a quote is left untouched so the
        # rest of the pipeline (fuzzy match at 0.7 cutoff) can still
        # recover the obvious target.
        assert repair('"terminal"') == "terminal"
