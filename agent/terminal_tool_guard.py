"""Bounded terminal-tool contract for delegated agent runs.

The ordinary intent-ack detector is deliberately heuristic. Dispatch systems
need a stronger, generic condition: a worker run is not complete until it has
emitted an auditable terminal tool result. Nothing here is specific to Swarm,
SLC, or MCP.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence


def _as_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, Sequence):
        values = value
    else:
        values = ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    try:
                        parts.append(json.dumps(item, ensure_ascii=False))
                    except (TypeError, ValueError):
                        parts.append(str(item))
        return "\n".join(parts)
    return str(value or "")


def _tool_result_failed(message: Mapping[str, Any]) -> bool:
    if message.get("is_error") is True or message.get("isError") is True:
        return True
    content = message.get("content")
    if isinstance(content, Mapping):
        return bool(content.get("is_error") is True or content.get("isError") is True)
    if not isinstance(content, str):
        return False
    normalized = content.strip().lower()
    if normalized.startswith(("error:", "tool error:", "failed:")):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, Mapping) and bool(
        payload.get("is_error") is True
        or payload.get("isError") is True
        or payload.get("success") is False
    )


def successful_terminal_tool(
    *,
    messages: Sequence[Mapping[str, Any]],
    required_tools: Sequence[str],
    result_pattern: str = "",
) -> Optional[str]:
    """Return the required tool with a successful, semantically final result."""

    required = set(required_tools)
    compiled_pattern: Optional[re.Pattern[str]] = None
    if result_pattern:
        try:
            compiled_pattern = re.compile(result_pattern)
        except re.error:
            # A malformed completion contract must fail closed: accepting an
            # arbitrary successful result can silently terminate delegated
            # work before the external system reaches its terminal state.
            return None
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        name = str(message.get("name") or message.get("tool_name") or "").strip()
        if name not in required or _tool_result_failed(message):
            continue
        if compiled_pattern is not None and compiled_pattern.search(
            _message_text(message.get("content"))
        ) is None:
            continue
        return name
    return None


def _active_required_tools(
    *,
    agent: Any,
    user_message: Any,
    messages: Sequence[Mapping[str, Any]],
    current_turn_user_idx: int,
) -> tuple[tuple[str, ...], int]:
    required = _as_names(getattr(agent, "_required_terminal_tools", ()))
    if not required:
        return (), 0
    platforms = _as_names(getattr(agent, "_required_terminal_tool_platforms", ()))
    if platforms and str(getattr(agent, "platform", "") or "") not in platforms:
        return (), 0
    pattern = str(
        getattr(agent, "_required_terminal_tool_user_pattern", "") or ""
    ).strip()
    if pattern:
        try:
            if re.search(pattern, _message_text(user_message)) is None:
                return (), 0
        except re.error:
            return (), 0
    start = max(0, min(int(current_turn_user_idx or 0), len(messages)))
    result_pattern = str(
        getattr(agent, "_required_terminal_tool_result_pattern", "") or ""
    ).strip()
    if successful_terminal_tool(
        messages=messages[start:],
        required_tools=required,
        result_pattern=result_pattern,
    ) is not None:
        return (), start
    return required, start


def _failed_skill_patch_target(
    *,
    messages: Sequence[Mapping[str, Any]],
    tool_result_index: int,
    required_tools: Sequence[str],
) -> Optional[str]:
    result = messages[tool_result_index]
    if not _tool_result_failed(result) or tool_result_index <= 0:
        return None
    assistant = messages[tool_result_index - 1]
    if not isinstance(assistant, Mapping) or assistant.get("role") != "assistant":
        return None
    tool_calls = assistant.get("tool_calls")
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, str):
        return None

    result_call_id = str(result.get("tool_call_id") or "").strip()
    aliases = {name.rsplit("__", 1)[-1]: name for name in required_tools}
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            continue
        function = tool_call.get("function")
        if not isinstance(function, Mapping):
            continue
        if str(function.get("name") or "").strip() != "skill_manage":
            continue
        call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
        if result_call_id and call_id and result_call_id != call_id:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                continue
        if not isinstance(arguments, Mapping):
            continue
        if str(arguments.get("action") or "").strip() != "patch":
            continue
        target = str(arguments.get("name") or "").strip()
        if target in aliases:
            return aliases[target]
    return None


def forced_terminal_tool_after_repeated_searches(
    *,
    agent: Any,
    user_message: Any,
    messages: Sequence[Mapping[str, Any]],
    current_turn_user_idx: int = 0,
) -> Optional[str]:
    """Return a required tool after repeated exact search or misdirection."""

    try:
        threshold = max(
            0,
            int(getattr(agent, "_required_terminal_tool_force_after_searches", 2)),
        )
    except (TypeError, ValueError):
        threshold = 2
    if threshold == 0:
        return None
    required, start = _active_required_tools(
        agent=agent,
        user_message=user_message,
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
    )
    if not required:
        return None

    counts = {name: 0 for name in required}
    turn_messages = messages[start:]
    for index in range(len(turn_messages) - 1, -1, -1):
        message = turn_messages[index]
        if not isinstance(message, Mapping):
            break
        role = message.get("role")
        if role == "assistant":
            if message.get("tool_calls"):
                continue
            break
        if role != "tool":
            break
        name = str(message.get("name") or message.get("tool_name") or "").strip()
        candidate: Optional[str] = None
        if name == "tool_search":
            try:
                payload = json.loads(str(message.get("content") or ""))
            except (TypeError, ValueError):
                break
            query = (
                str(payload.get("query") or "").strip()
                if isinstance(payload, Mapping)
                else ""
            )
            if query in counts:
                candidate = query
        elif name == "skill_manage":
            candidate = _failed_skill_patch_target(
                messages=turn_messages,
                tool_result_index=index,
                required_tools=required,
            )
        else:
            break
        if candidate not in counts:
            break
        counts[candidate] += 1
        if counts[candidate] >= threshold:
            return candidate
    return None


def terminal_tool_request_scope(
    *, messages: Sequence[Mapping[str, Any]], required_tool: str
) -> frozenset[str]:
    """Keep the forced tool plus every function referenced by the dialog."""

    names = {str(required_tool or "").strip()}
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, str):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            function = tool_call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = str(function.get("name") or "").strip()
            if name:
                names.add(name)
    names.discard("")
    return frozenset(names)


def build_required_terminal_tool_nudge(
    *,
    agent: Any,
    user_message: Any,
    messages: Sequence[Mapping[str, Any]],
    attempts: int,
    current_turn_user_idx: int = 0,
) -> Optional[str]:
    """Return a continuation nudge while the completion contract is unmet."""

    try:
        max_nudges = max(
            0, int(getattr(agent, "_required_terminal_tool_max_nudges", 2))
        )
    except (TypeError, ValueError):
        max_nudges = 2
    if attempts >= max_nudges:
        return None
    required, _ = _active_required_tools(
        agent=agent,
        user_message=user_message,
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
    )
    if not required:
        return None
    custom = str(getattr(agent, "_required_terminal_tool_nudge", "") or "").strip()
    if custom:
        return custom
    names = ", ".join(required)
    return (
        "[System: This delegated run is not complete because its required "
        f"terminal tool has not succeeded: {names}. Continue executing actual "
        "tools now; if a configured tool is hidden by lazy loading, use exact-name "
        "tool discovery first. Do not answer with a plan, tutorial, simulation, "
        "or promise to continue. When complete or genuinely blocked, call the "
        "required terminal tool with concise evidence.]"
    )
