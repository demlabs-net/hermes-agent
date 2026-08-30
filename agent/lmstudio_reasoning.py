"""LM Studio reasoning-effort resolution shared by the chat-completions
transport and run_agent's iteration-limit summary path.

LM Studio publishes per-model ``capabilities.reasoning.allowed_options`` (e.g.
``["off","on"]`` for toggle-style models, ``["off","minimal","low"]`` for
graduated models). LM Studio's OpenAI-compatible endpoint still accepts its
generic effort vocabulary rather than those literal toggle values. We map and
clamp without sending a graded setting to a binary model, which would make LM
Studio warn and silently fall back on every request.
"""

from __future__ import annotations

from typing import List, Optional

# LM Studio accepts these top-level reasoning_effort values via its
# OpenAI-compatible chat.completions endpoint.
_LM_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

# Toggle-style models publish allowed_options as ["off","on"] in /api/v1/models.
# Map them onto the OpenAI-compatible request vocabulary for graduated models;
# the binary-only case is handled separately below.
_LM_EFFORT_ALIASES = {"off": "none", "on": "medium"}

# Hermes' generic effort ladder grew past LM Studio's vocabulary ("max",
# "ultra"). Clamp the stronger generic levels onto LM Studio's ceiling: left
# alone they miss _LM_VALID_EFFORTS, keep the initialized "medium" default and
# are thereby conflated with unparseable input, so asking for more reasoning
# yields less than "xhigh". Mirrors the ceiling clamp every other provider
# applies (see agent/transports/codex.py).
#
# Deliberately separate from _LM_EFFORT_ALIASES: that mapping is also applied
# to the model's published allowed_options, which must not be rewritten.
_LM_EFFORT_CLAMP = {"max": "xhigh", "ultra": "xhigh"}


def resolve_lmstudio_effort(
    reasoning_config: Optional[dict],
    allowed_options: Optional[List[str]],
) -> Optional[str]:
    """Return the ``reasoning_effort`` string to send to LM Studio, or ``None``.

    ``None`` means "omit the field": the user picked a level the model can't
    honor, so let LM Studio fall back to the model's declared default rather
    than silently substituting a different effort. When ``allowed_options`` is
    falsy (probe failed), skip clamping and send the resolved effort anyway.
    """
    effort = "medium"
    if reasoning_config and isinstance(reasoning_config, dict):
        if reasoning_config.get("enabled") is False:
            effort = "none"
        else:
            raw = (reasoning_config.get("effort") or "").strip().lower()
            raw = _LM_EFFORT_ALIASES.get(raw, raw)
            raw = _LM_EFFORT_CLAMP.get(raw, raw)
            if raw in _LM_VALID_EFFORTS:
                effort = raw
    if allowed_options:
        published = {
            str(option).strip().lower()
            for option in allowed_options
            if str(option).strip()
        }
        # ``/v1/chat/completions`` rejects literal ``on``/``off`` even when
        # model metadata publishes only those options. Omit the field for an
        # enabled binary model so its declared default applies without a
        # warning. The endpoint accepts ``none`` for an explicit disable.
        if "on" in published and published.issubset({"off", "on"}):
            if effort == "none":
                return "none" if "off" in published else None
            return None
        allowed = {_LM_EFFORT_ALIASES.get(opt, opt) for opt in published}
        if effort not in allowed:
            return None
    return effort
