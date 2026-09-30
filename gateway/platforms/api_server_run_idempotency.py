"""Durable idempotency reservations for API server runs."""

import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict


# Keep the extracted store's log records on the API server logger.
logger = logging.getLogger("gateway.platforms.api_server")

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

_SELECT_BY_KEY = (
    "SELECT fingerprint, run_id, status_json, owner_pid, owner_started, updated_at "
    "FROM run_idempotency WHERE scope=? AND idempotency_key=?")
_EXTEND_RETENTION_BY_KEY = (
    "UPDATE run_idempotency SET retention_until=MAX(retention_until, ?) "
    "WHERE scope=? AND idempotency_key=? AND fingerprint=?")
_EXTEND_RETENTION_BY_RUN = (
    "UPDATE run_idempotency SET retention_until=MAX(retention_until, ?) "
    "WHERE scope=? AND run_id=?")
# Columns added after the first schema shipped; applied when missing.
_MIGRATIONS = {
    "owner_pid": "INTEGER NOT NULL DEFAULT 0",
    "owner_started": "INTEGER NOT NULL DEFAULT 0",
    "retention_until": "REAL NOT NULL DEFAULT 0",
    "acknowledged_at": "REAL"}


def _encode_status(status: Dict[str, Any]) -> str:
    return json.dumps(status, sort_keys=True, separators=(",", ":"))


def _record(run_id, status_json, owner_pid, owner_started, updated_at) -> dict[str, Any]:
    return {
        "run_id": run_id, "status": json.loads(status_json), "owner_pid": int(owner_pid or 0),
        "owner_started": int(owner_started or 0), "updated_at": float(updated_at or 0)}


def _outcome(row, fingerprint):
    """Classify a stored ``(scope, key)`` row against the caller's fingerprint."""
    return ("reused" if hmac.compare_digest(row[0], fingerprint) else "conflict"), _record(*row[1:])


class RunIdempotencyStore:
    """Durable, tenant-scoped reservations for ``POST /v1/runs``: a unique ``(scope, key)`` row
    inserted inside ``BEGIN IMMEDIATE`` so separate workers cannot both admit one request. Only
    fingerprints and public run status are stored — never request bodies or credentials."""

    RETENTION_SECONDS = 24 * 60 * 60
    ACKNOWLEDGED_RETENTION_SECONDS = 24 * 60 * 60

    @property
    def durable(self) -> bool:
        """Whether reservations survive this process."""
        return self._db_path is not None
    def __init__(self, db_path: str = None):
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "runs_idempotency.db")
            except Exception:
                db_path = ":memory:"
        self._db_path = None if db_path == ":memory:" else db_path
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        except Exception as exc:
            # Docker may create the container object before `docker run` fails to start it (e.g. exit code
            # 125 when the daemon isn't ready, or a timeout mid-pull). That orphan is left in "Created"
            # state — which the exited-only orphan reaper (reap_orphan_containers, status=exited) never
            # catches, so it leaks permanently. Remove it by its known name before re-raising. See #7439.
            logger.warning(
                "Run idempotency storage is unavailable; falling back to "
                "process memory, so replay will not survive a restart: %s", exc)
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        from hermes_state_wal import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="runs_idempotency.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS run_idempotency (
                scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status_json TEXT NOT NULL,
                owner_pid INTEGER NOT NULL DEFAULT 0,
                owner_started INTEGER NOT NULL DEFAULT 0,
                retention_until REAL NOT NULL DEFAULT 0,
                acknowledged_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (scope, idempotency_key)
            )"""
        )
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(run_idempotency)")}
        for column, ddl in _MIGRATIONS.items():
            if column not in columns:
                self._conn.execute(f"ALTER TABLE run_idempotency ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS run_idempotency_run_id ON run_idempotency(run_id)")
        # Workflow identity is permanent: retrying an old task/attempt must never
        # launch a second executor, even after ordinary run retention expires.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS workflow_run_binding (
                attempt_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                scope TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                status_json TEXT NOT NULL,
                registry_json TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                owner_started INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (task_id, attempt)
            )""")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS workflow_abort (
                attempt_id TEXT NOT NULL PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                scope TEXT NOT NULL,
                transport_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                registry_json TEXT NOT NULL,
                aborted_at REAL NOT NULL,
                UNIQUE (task_id, attempt),
                UNIQUE (scope, transport_key)
            )""")
        self._conn.commit()
        self._lock = threading.Lock()
        self._tighten_permissions()

    def _tighten_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm") if self._db_path else ():
            candidate = Path(self._db_path + suffix)
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug("Failed to restrict run idempotency store permissions", exc_info=True)

    @contextmanager
    def _immediate_txn(self):
        """Hold the lock inside ``BEGIN IMMEDIATE``; the body commits, errors roll back."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.rollback()
                raise

    def reserve(self, scope: str, key: str, fingerprint: str, run_id: str, status: Dict[str, Any], *,
                owner_pid: int = 0, owner_started: int = 0, retention_until: float = 0):
        """Atomically reserve a key; return ``(outcome, stored_record)``."""
        now = time.time()
        retention_until = max(0.0, float(retention_until or 0))
        encoded = _encode_status(status)
        with self._immediate_txn():
            self._prune_stale_terminal_locked(now)
            row = self._conn.execute(_SELECT_BY_KEY, (scope, key)).fetchone()
            if row is not None:
                if retention_until:
                    self._conn.execute(_EXTEND_RETENTION_BY_KEY, (retention_until, scope, key, fingerprint))
                self._conn.commit()
                return _outcome(row, fingerprint)
            self._conn.execute(
                "INSERT INTO run_idempotency("
                "scope,idempotency_key,fingerprint,run_id,status_json,"
                "owner_pid,owner_started,retention_until,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (scope, key, fingerprint, run_id, encoded, int(owner_pid or 0), int(owner_started or 0),
                 retention_until, now, now))
            self._conn.commit()
            return "created", _record(run_id, encoded, owner_pid, owner_started, now) | {"status": status}

    def lookup(self, scope: str, key: str, fingerprint: str, *, retention_until: float = 0):
        """Return ``missing``, ``reused`` or ``conflict`` without reserving."""
        now = time.time()
        retention_until = max(0.0, float(retention_until or 0))
        with self._immediate_txn():
            if retention_until:
                self._conn.execute(_EXTEND_RETENTION_BY_KEY, (retention_until, scope, key, fingerprint))
            self._prune_stale_terminal_locked(now)
            row = self._conn.execute(_SELECT_BY_KEY, (scope, key)).fetchone()
            self._conn.commit()
        return ("missing", None) if row is None else _outcome(row, fingerprint)

    def _prune_stale_terminal_locked(self, now: float) -> None:
        """Prune aged replay records only once their stored run is terminal (caller holds the
        lock + transaction): a long or disconnected room turn may outlive the retention window."""
        stale = self._conn.execute(
            """SELECT scope, idempotency_key, status_json
                 FROM run_idempotency
                WHERE acknowledged_at <= ?
                   OR (retention_until > 0 AND retention_until <= ?)
                   OR (retention_until <= 0 AND updated_at < ?)""",
            (now - self.ACKNOWLEDGED_RETENTION_SECONDS, now, now - self.RETENTION_SECONDS),
        ).fetchall()
        for stale_scope, stale_key, stale_status in stale:
            try:
                terminal = json.loads(stale_status).get("status") in TERMINAL_STATUSES
            except Exception:
                terminal = False
            if terminal:
                self._conn.execute(
                    "DELETE FROM run_idempotency WHERE scope=? AND idempotency_key=?", (stale_scope, stale_key))

    def status_for_run(self, scope: str, run_id: str, *, retention_until: float = 0) -> dict[str, Any] | None:
        """Load one durable run status inside its authenticated scope."""
        retention_until = max(0.0, float(retention_until or 0))
        with self._lock:
            if retention_until:
                self._conn.execute(_EXTEND_RETENTION_BY_RUN, (retention_until, scope, run_id))
                self._conn.commit()
            row = self._conn.execute(
                "SELECT status_json, owner_pid, owner_started, updated_at "
                "FROM run_idempotency WHERE scope=? AND run_id=?",
                (scope, run_id)).fetchone()
        if row is None:
            return None
        return {k: v for k, v in _record(None, *row).items() if k != "run_id"}

    def extend_retention(self, scope: str, run_id: str, until: float) -> bool:
        """Persist the latest verified recovery horizon for an active grant."""
        checked_until = max(0.0, float(until or 0))
        if not checked_until:
            return False
        with self._lock:
            changed = self._conn.execute(_EXTEND_RETENTION_BY_RUN, (checked_until, scope, run_id)).rowcount
            self._conn.commit()
        return changed == 1

    def owns_run(self, scope: str, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM run_idempotency WHERE scope=? AND run_id=?", (scope, run_id)).fetchone()
        return row is not None

    def update_status(self, run_id: str, status: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_idempotency SET status_json=?, updated_at=? WHERE run_id=?",
                (_encode_status(status), time.time(), run_id))
            self._conn.commit()

    def reserve_workflow(
        self, task_id: str, attempt: int, scope: str, fingerprint: str, run_id: str,
        status: Dict[str, Any], registry: Dict[str, Any], *, owner_pid: int, owner_started: int,
    ):
        """Bind the already-authorized task/attempt before any executor is created.

        This is a storage operation, NOT an authorization decision: the caller must
        validate the issuer's started-event ticket before calling it. Unlike ordinary
        idempotency, workflow bindings are never pruned and never fall back to RAM.
        """
        if not self.durable:
            raise RuntimeError("Workflow run binding requires durable SQLite storage")
        now = time.time()
        encoded = _encode_status(status)
        with self._immediate_txn():
            aborted = self._lookup_workflow_abort_locked(
                registry["attempt_id"], task_id, attempt, scope, registry["transport_key"])
            if aborted is not None:
                self._conn.commit()
                return "aborted", aborted
            row = self._conn.execute(
                "SELECT scope, fingerprint, run_id, status_json, owner_pid, owner_started, updated_at, "
                "registry_json FROM workflow_run_binding WHERE task_id=? AND attempt=? OR attempt_id=?",
                (task_id, attempt, registry["attempt_id"]),
            ).fetchone()
            if row is not None:
                self._conn.commit()
                bound = json.loads(row[7])
                same = (hmac.compare_digest(row[0], scope) and hmac.compare_digest(row[1], fingerprint)
                        and bound["attempt_id"] == registry["attempt_id"]
                        and bound["task_id"] == task_id and bound["attempt_no"] == attempt)
                return ("reused" if same else "conflict"), _record(*row[2:7])
            self._conn.execute(
                "INSERT INTO workflow_run_binding (attempt_id, task_id, attempt, scope, fingerprint, run_id, "
                "status_json, registry_json, owner_pid, owner_started, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (registry["attempt_id"], task_id, attempt, scope, fingerprint, run_id, encoded,
                 json.dumps(registry, sort_keys=True, separators=(",", ":")),
                 int(owner_pid), int(owner_started), now, now),
            )
            self._conn.commit()
            return "created", _record(run_id, encoded, owner_pid, owner_started, now)

    def _lookup_workflow_abort_locked(self, attempt_id: str, task_id: str, attempt: int,
                                      scope: str, transport_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT registry_json FROM workflow_abort WHERE attempt_id=? OR (task_id=? AND attempt=?) "
            "OR (scope=? AND transport_key=?)",
            (attempt_id, task_id, attempt, scope, transport_key)).fetchone()
        return json.loads(row[0]) if row else None

    def abort_workflow(self, registry: Dict[str, Any], scope: str, fingerprint: str):
        """Fence an attempt before admission; the committed tombstone is never pruned.

        The same SQLite writer transaction as reserve_workflow chooses the winner,
        across threads, connections, and gateway processes. Never claim no run
        unless the tombstone transaction committed on durable storage.
        """
        if not self.durable:
            raise RuntimeError("Workflow abort requires durable SQLite storage")
        attempt_id, task_id, attempt = (registry[k] for k in ("attempt_id", "task_id", "attempt_no"))
        with self._immediate_txn():
            admitted = self._conn.execute(
                "SELECT registry_json FROM workflow_run_binding WHERE attempt_id=? OR (task_id=? AND attempt=?) "
                "OR (scope=? AND json_extract(registry_json, '$.transport_key')=?)",
                (attempt_id, task_id, attempt, scope, registry["transport_key"])).fetchone()
            if admitted is not None:
                self._conn.commit()
                return "admitted", json.loads(admitted[0])
            aborted = self._lookup_workflow_abort_locked(
                attempt_id, task_id, attempt, scope, registry["transport_key"])
            if aborted is not None:
                self._conn.commit()
                same = (aborted["attempt_id"] == attempt_id and aborted["task_id"] == task_id
                        and aborted["attempt_no"] == attempt and aborted["profile"] == registry["profile"]
                        and aborted["transport_key"] == registry["transport_key"]
                        and all(aborted[key] == registry[key] for key in registry))
                return ("aborted" if same else "conflict"), aborted
            now = time.time()
            receipt = dict(registry, run_id=None, status="aborted", active=False,
                           aborted=True, aborted_at=now, abort_receipt="abort_" + uuid.uuid4().hex)
            self._conn.execute(
                "INSERT INTO workflow_abort (attempt_id, task_id, attempt, scope, transport_key, "
                "fingerprint, registry_json, aborted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, task_id, attempt, scope, registry["transport_key"], fingerprint,
                 json.dumps(receipt, sort_keys=True, separators=(",", ":")), now))
            self._conn.commit()
            return "aborted", receipt

    def lookup_workflow(self, task_id: str, attempt: int, attempt_id: str, scope: str, fingerprint: str,
                        transport_key: str = ""):
        """Resolve unknown-outcome POSTs before checking the concurrency limit."""
        if not self.durable:
            raise RuntimeError("Workflow run binding requires durable SQLite storage")
        with self._lock:
            aborted = self._lookup_workflow_abort_locked(attempt_id, task_id, attempt, scope, transport_key)
            row = self._conn.execute(
                "SELECT scope, fingerprint, run_id, status_json, owner_pid, owner_started, updated_at, "
                "registry_json FROM workflow_run_binding WHERE task_id=? AND attempt=? OR attempt_id=?",
                (task_id, attempt, attempt_id)).fetchone()
        if aborted is not None:
            return "aborted", aborted
        if row is None:
            return "missing", None
        same = (hmac.compare_digest(row[0], scope) and hmac.compare_digest(row[1], fingerprint)
                and json.loads(row[7])["attempt_id"] == attempt_id)
        return ("reused" if same else "conflict"), _record(*row[2:7])

    def workflow_registry(self, attempt_id: str) -> dict[str, Any] | None:
        """Return an immutable, durable admission record for the SLC bind verifier."""
        if not self.durable:
            raise RuntimeError("Workflow registry requires durable SQLite storage")
        with self._lock:
            row = self._conn.execute(
                "SELECT registry_json FROM workflow_run_binding WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT registry_json FROM workflow_abort WHERE attempt_id=?", (attempt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def workflow_registry_for_run(self, scope: str, run_id: str) -> dict[str, Any] | None:
        if not self.durable:
            raise RuntimeError("Workflow registry requires durable SQLite storage")
        with self._lock:
            row = self._conn.execute(
                "SELECT registry_json FROM workflow_run_binding WHERE scope=? AND run_id=?",
                (scope, run_id)).fetchone()
        return json.loads(row[0]) if row else None

    def workflow_status_for_run(self, scope: str, run_id: str) -> dict[str, Any] | None:
        if not self.durable:
            raise RuntimeError("Workflow registry requires durable SQLite storage")
        with self._lock:
            row = self._conn.execute(
                "SELECT status_json, owner_pid, owner_started, updated_at "
                "FROM workflow_run_binding WHERE scope=? AND run_id=?", (scope, run_id),
            ).fetchone()
        return {k: v for k, v in _record(None, *row).items() if k != "run_id"} if row else None

    def update_workflow_status(self, run_id: str, status: Dict[str, Any], *, active: bool) -> None:
        """Persist terminal inactivity only after the executor's cleanup has completed."""
        with self._immediate_txn():
            row = self._conn.execute(
                "SELECT registry_json FROM workflow_run_binding WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise RuntimeError("Workflow binding missing")
            registry = json.loads(row[0])
            registry["status"] = status["status"]
            registry["active"] = bool(active)
            self._conn.execute(
                "UPDATE workflow_run_binding SET status_json=?, registry_json=?, updated_at=? WHERE run_id=?",
                (_encode_status(status), json.dumps(registry, sort_keys=True, separators=(",", ":")),
                 time.time(), run_id))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
