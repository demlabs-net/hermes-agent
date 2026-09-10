"""Operator-owned stop control for every agent run.

The swarm and SLC holds gate their own transport, but a cron fire, an
API-server run or a platform webhook starts a conversation directly and would
bypass them. This module is the single chokepoint those paths consult, so an
operator can stop all agent work without stopping the container.

Authority is deliberately outside the model's reach:

* ``HERMES_OPERATOR_HOLD`` — process environment, set by the operator and
  re-read from the container's env file on restart;
* ``HERMES_OPERATOR_HOLD_FILE`` — a marker file, expected to live on a
  read-only mount so an agent's shell tools cannot clear it.

Both are deployment-owned. No tool, prompt or agent instruction may clear a
hold; that is the point of the control. Health, dashboards, reporting and
read-only inspection are not gated here because they never call
:func:`require_released`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

#: Default marker path; deployments mount a read-only directory here.
DEFAULT_HOLD_FILE = "/opt/hermes/operator-hold/active"

#: Marker prefix used in skips so operators can filter the reason in logs.
HOLD_MARKER = "[operator_hold]"

_TRUTHY = {"1", "true", "yes", "on"}

_REMEDIATION = (
    "Agent work is stopped by the operator. Start the run only after the operator "
    "releases the hold (unset HERMES_OPERATOR_HOLD and remove the hold marker); "
    "queued or historical instructions do not release it."
)


class OperatorHoldError(RuntimeError):
    """Raised when a run is attempted while the operator hold is active."""


def _env_held() -> bool:
    return os.getenv("HERMES_OPERATOR_HOLD", "").strip().lower() in _TRUTHY


def hold_file_path() -> Path:
    configured = os.getenv("HERMES_OPERATOR_HOLD_FILE", "").strip()
    return Path(configured or DEFAULT_HOLD_FILE)


def hold_state() -> Tuple[bool, str]:
    """Return ``(held, reason)`` for the current process environment.

    An unreadable marker is treated as held: a stop control must fail closed,
    otherwise a permissions or mount fault would silently allow work.
    """
    if _env_held():
        return True, "HERMES_OPERATOR_HOLD is set"
    path = hold_file_path()
    try:
        if path.exists():
            try:
                reason = path.read_text(encoding="utf-8").strip()
            except OSError:
                reason = ""
            return True, reason or "operator hold marker present: %s" % path
    except OSError:
        return True, "cannot read operator hold marker %s; failing closed" % path
    return False, ""


def hold_message() -> str:
    held, reason = hold_state()
    return "%s %s. %s" % (HOLD_MARKER, reason, _REMEDIATION) if held else ""


def require_released(context: str = "agent run") -> None:
    """Raise :class:`OperatorHoldError` when the hold blocks ``context``."""
    held, reason = hold_state()
    if held:
        raise OperatorHoldError(
            "%s %s is blocked: %s. %s" % (HOLD_MARKER, context, reason, _REMEDIATION)
        )


def is_held() -> bool:
    """Non-raising check for callers that must report status instead of failing."""
    return hold_state()[0]
