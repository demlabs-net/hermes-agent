"""Operator-owned admission barriers (not cancellation of dispatched I/O).

Sales deployments explicitly set HERMES_OPERATOR_HOLD_FILE to the legacy marker
on a read-only directory mount. Its sibling control.json MUST contain
{"generation": "unique opaque token", "held": false}. Operators atomically
replace that file with a fresh generation on EVERY hold/release, never delete it.
The same file carries the operator's cron approvals: authorized_cron_jobs (one
payload digest per job id) and authorized_cron_extras (per job id, the digests of
the per-fire instruction texts the operator approved). The marker and environment
remain additional stop controls. A successful check
is the admission linearization point: already admitted HTTP/tools may finish.
Unconfigured desktop installations retain optional legacy-marker behavior.
"""
from __future__ import annotations

import os
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOLD_FILE = "/opt/hermes/operator-hold/active"
HOLD_MARKER = "[operator_hold]"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"", "0", "false", "no", "off", "disabled"}
_REMEDIATION = (
    "Agent work is stopped by the operator. Start a new run only after the operator "
    "releases the hold and rotates control.json generation; "
    "queued or historical instructions do not release it."
)

class OperatorHoldError(RuntimeError):
    """An admission was refused; retrying this run cannot restore its grant."""


@dataclass(frozen=True)
class OperatorPolicy:
    marker: Path
    configured: bool
    startup_hold: str


# Imported at agent-package bootstrap, before any model/tool execution. Never
# re-resolve authority paths from mutable per-tool/profile process environment.
_CONFIGURED_PATH = os.getenv("HERMES_OPERATOR_HOLD_FILE", "").strip()
_POLICY = OperatorPolicy(Path(_CONFIGURED_PATH or DEFAULT_HOLD_FILE), bool(_CONFIGURED_PATH),
                         os.getenv("HERMES_OPERATOR_HOLD", "").strip().lower())


def configured_policy() -> bool:
    return _POLICY.configured


def hold_file_path() -> Path:
    return _POLICY.marker


def _snapshot():
    path = hold_file_path()
    configured = configured_policy()
    flags = (_POLICY.startup_hold, os.getenv("HERMES_OPERATOR_HOLD", "").strip().lower())
    if any(flag in _TRUTHY or (configured and flag not in _FALSEY) for flag in flags):
        return True, "HERMES_OPERATOR_HOLD is set or invalid", None
    try:
        # lstat, not exists(): exists can suppress filesystem errors. A dangling
        # symlink is a present marker too. Only ENOENT means absent.
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            try:
                reason = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                reason = ""
            return True, reason or f"operator hold marker present: {path}", None
        control = path.parent / "control.json"
        try:
            raw = control.read_text(encoding="utf-8")
        except FileNotFoundError:
            if configured:
                raise
            return False, "", (str(path), "legacy")
        data = json.loads(raw)
        generation = data.get("generation")
        if not isinstance(generation, str) or not generation.strip() or type(data.get("held")) is not bool:
            raise ValueError("invalid control schema")
        return data["held"], "operator control held" if data["held"] else "", (str(path), generation)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        return True, f"cannot read operator control; failing closed ({type(exc).__name__})", None


def hold_state():
    held, reason, _ = _snapshot()
    return held, reason


def hold_message() -> str:
    held, reason = hold_state()
    return f"{HOLD_MARKER} {reason}. {_REMEDIATION}" if held else ""


@dataclass(frozen=True)
class RunGrant:
    generation: tuple
    # Shared across context copies: once any worker observes denial all siblings
    # remain revoked, even if a legacy marker is subsequently removed.
    revoked: threading.Event = field(default_factory=threading.Event, compare=False)


_grant: ContextVar[RunGrant | None] = ContextVar("operator_run_grant", default=None)


def require_released(context: str = "agent run") -> None:
    held, reason, generation = _snapshot()
    grant = _grant.get()
    if grant is not None and (grant.revoked.is_set() or grant.generation != generation):
        held, reason = True, "run generation revoked"
    if held:
        if grant is not None:
            grant.revoked.set()
        raise OperatorHoldError(f"{HOLD_MARKER} {context} is blocked: {reason}. {_REMEDIATION}")


@contextmanager
def run_scope():
    """Capture once; nested/delegated runs must inherit, never renew authority."""
    if _grant.get() is not None:
        require_released()
        yield _grant.get()
        return
    held, reason, generation = _snapshot()
    if held:
        raise OperatorHoldError(f"{HOLD_MARKER} {reason}. {_REMEDIATION}")
    grant = RunGrant(generation)
    token = _grant.set(grant)
    try:
        require_released()
        yield grant
    finally:
        _grant.reset(token)


def require_generation(generation: str | None) -> None:
    """Bind delayed ingress to operator authorization, not arrival time."""
    if not configured_policy() and generation is None:
        return
    require_released("generation admission")
    grant = _grant.get()
    current = grant.generation if grant is not None else _snapshot()[2]
    if not generation or current is None or generation != current[1]:
        raise OperatorHoldError(f"{HOLD_MARKER} missing or stale operator generation")


def guarded_call(callback, *args, **kwargs):
    """Check immediately before dispatch, including after queue/approval waits."""
    require_released("external dispatch")
    return callback(*args, **kwargs)


def is_held() -> bool:
    return hold_state()[0]


def cron_payload_digest(job):
    import hashlib
    # Script/monitor execution and dynamic skill/context imports are deliberately
    # not authorizable here: their referenced content can mutate independently.
    if any(job.get(k) for k in ("script", "monitor_script", "monitor_url", "skills", "skill", "context_from")):
        raise ValueError("configured cron requires a self-contained prompt (no scripts/skills/context imports)")
    # Bind every non-runtime field, including future tool/provider permissions.
    # The engine also mutates its own bookkeeping between approval and the next fire: the
    # due scan persists last_dispatch, the claim adds _scheduled_instant, a run bumps the
    # repeat/failure counters. Binding those made every approved job unauthorized on its
    # first fire, so they belong to the runtime set, not to the payload.
    runtime = {"operator_generation", "next_run_at", "last_run_at", "last_status", "last_error",
               "run_count", "state", "enabled", "paused_at", "paused_reason", "updated_at",
               "run_claim", "fire_claim", "last_output", "last_response",
               "_scheduled_instant", "execution_id", "latest_execution", "last_dispatch",
               "last_delivery_queued", "last_delivery_error", "last_fire_error",
               "failure_streak", "preflight_alerted", "manual_run_at", "manual_run_prompt"}
    payload = {k: v for k, v in job.items() if k not in runtime}
    # ``repeat`` mixes the operator-approved run limit with a run counter: keep ``times``
    # bound, drop ``completed`` — otherwise each finished run revoke its own approval.
    if isinstance(payload.get("repeat"), dict):
        payload["repeat"] = {"times": payload["repeat"].get("times")}
    payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def extra_prompt_digest(text: str) -> str:
    """Digest of one exact per-fire instruction block the operator approved.

    Bound separately from the job payload: a job approval must never carry whatever
    instructions a later trigger happens to attach, and re-approving the same text must
    stay valid across the single-fire stamps the engine adds and removes.
    """
    import hashlib
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def require_cron_authorization(job, extra_prompt: str | None = None):
    if not configured_policy():
        return
    require_generation(job.get("operator_generation"))
    try:
        data = json.loads((hold_file_path().parent / "control.json").read_text())
        digest = data.get("authorized_cron_jobs", {}).get(job["id"])
        if digest != cron_payload_digest(job):
            raise ValueError("job payload not operator authorized")
        approved_extras = (data.get("authorized_cron_extras") or {}).get(job["id"]) or []
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise OperatorHoldError(f"{HOLD_MARKER} cron authorization denied: {exc}") from exc
    # A per-fire extra prompt carries fresh instructions, so an authorized job never covers
    # it: the operator approves that exact text by digest, host-side, and nothing else does.
    if extra_prompt and extra_prompt_digest(extra_prompt) not in approved_extras:
        raise OperatorHoldError(f"{HOLD_MARKER} per-fire extra_prompt is not operator authorized")
    require_released("cron authorization")


def scoped_run(callback):
    """Preserve admission across scheduler setup and its eventual conversation."""
    from functools import wraps

    @wraps(callback)
    def wrapped(*args, **kwargs):
        try:
            with run_scope():
                job = args[0] if args else kwargs["job"]
                # A per-fire extra_prompt stays admissible, but only with its own approval:
                # the check below refuses one the operator never approved by digest.
                require_cron_authorization(job, kwargs.get("extra_prompt"))
                return callback(*args, **kwargs)
        except OperatorHoldError as exc:
            return False, "", "", str(exc)
    return wrapped
