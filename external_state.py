"""External durable state for Hermes' built-in memory and writable skills.

The local filesystem remains the compatibility surface used by the existing
memory and skill implementations, but it becomes a process-scoped cache when
external state is enabled.  The durable source of truth is a generic text
object backend.  The built-in backend speaks MCP Streamable HTTP and therefore
works with SLC or any other MCP server that implements the four ``state_*``
tools documented in ``docs/external-state.md``.

No network access occurs unless ``external_state.backend`` is configured.
Failures are strict by default: Hermes must not silently create a second,
divergent local memory when the authoritative backend is unavailable.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib
import json
import logging
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


class ExternalStateError(RuntimeError):
    """Raised when authoritative external state cannot be read or written."""


@dataclass(frozen=True)
class StateObject:
    key: str
    content: str
    content_type: str = "text/plain; charset=utf-8"
    etag: str = ""


class TextStateBackend(ABC):
    """Minimal backend contract for namespaced, UTF-8 text objects."""

    @abstractmethod
    def get(self, namespace: str, key: str) -> Optional[StateObject]:
        raise NotImplementedError

    @abstractmethod
    def list(self, namespace: str, prefix: str = "") -> list[StateObject]:
        raise NotImplementedError

    @abstractmethod
    def put(
        self,
        namespace: str,
        key: str,
        content: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
        expected_etag: Optional[str] = None,
    ) -> StateObject:
        raise NotImplementedError

    @abstractmethod
    def delete(
        self,
        namespace: str,
        key: str,
        *,
        expected_etag: Optional[str] = None,
    ) -> bool:
        raise NotImplementedError


class McpTextStateBackend(TextStateBackend):
    """Text-state backend over standard MCP Streamable HTTP."""

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not url:
            raise ExternalStateError("external_state.mcp.url is empty")
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds
        self._request_id = 0
        self._session_id = ""
        self._initialized = False
        self._lock = threading.RLock()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _decode_response(body: bytes, content_type: str) -> Dict[str, Any]:
        text = body.decode("utf-8")
        if "text/event-stream" in content_type:
            payloads = [
                line[5:].strip()
                for line in text.splitlines()
                if line.startswith("data:") and line[5:].strip()
            ]
            if not payloads:
                raise ExternalStateError("MCP server returned an empty SSE response")
            text = payloads[-1]
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExternalStateError("MCP server returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ExternalStateError("MCP response must be a JSON object")
        return value

    def _post(self, message: Dict[str, Any], *, notification: bool = False) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                self._session_id = response.headers.get("Mcp-Session-Id", self._session_id)
                if notification:
                    if response.status not in {200, 202, 204}:
                        raise ExternalStateError(
                            f"MCP notification failed with HTTP {response.status}"
                        )
                    return {}
                return self._decode_response(
                    body, response.headers.get("Content-Type", "application/json")
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ExternalStateError(
                f"MCP request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ExternalStateError(f"MCP request failed: {exc.reason}") from exc

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "hermes-external-state", "version": "1"},
                },
            }
        )
        if "error" in response:
            raise ExternalStateError(f"MCP initialize failed: {response['error']}")
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            notification=True,
        )
        self._initialized = True

    def _call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._ensure_initialized()
            response = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        if "error" in response:
            raise ExternalStateError(f"MCP {name} failed: {response['error']}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise ExternalStateError(f"MCP {name} returned an invalid result")
        if result.get("isError"):
            raise ExternalStateError(f"MCP {name} returned a tool error: {result}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise ExternalStateError(f"MCP {name} returned no structured object")

    def get(self, namespace: str, key: str) -> Optional[StateObject]:
        result = self._call("state_get", {"namespace": namespace, "key": key})
        if not result.get("found"):
            return None
        return StateObject(
            key=key,
            content=str(result.get("content", "")),
            content_type=str(result.get("content_type", "text/plain; charset=utf-8")),
            etag=str(result.get("etag", "")),
        )

    def list(self, namespace: str, prefix: str = "") -> list[StateObject]:
        result = self._call(
            "state_list", {"namespace": namespace, "prefix": prefix, "limit": 1000}
        )
        objects: list[StateObject] = []
        for item in result.get("objects", []):
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            objects.append(
                StateObject(
                    key=item["key"],
                    content="",
                    content_type=str(
                        item.get("content_type", "text/plain; charset=utf-8")
                    ),
                    etag=str(item.get("etag", "")),
                )
            )
        return objects

    def put(
        self,
        namespace: str,
        key: str,
        content: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
        expected_etag: Optional[str] = None,
    ) -> StateObject:
        arguments: Dict[str, Any] = {
            "namespace": namespace,
            "key": key,
            "content": content,
            "content_type": content_type,
        }
        if expected_etag is not None:
            arguments["expected_etag"] = expected_etag
        result = self._call("state_put", arguments)
        return StateObject(
            key=key,
            content=content,
            content_type=content_type,
            etag=str(result.get("etag", "")),
        )

    def delete(
        self,
        namespace: str,
        key: str,
        *,
        expected_etag: Optional[str] = None,
    ) -> bool:
        arguments: Dict[str, Any] = {"namespace": namespace, "key": key}
        if expected_etag is not None:
            arguments["expected_etag"] = expected_etag
        result = self._call("state_delete", arguments)
        return bool(result.get("deleted"))


def _load_config() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    section = config.get("external_state", {})
    return section if isinstance(section, dict) else {}


def _resolve_headers(config: Mapping[str, Any]) -> Dict[str, str]:
    headers = {
        str(key): str(value)
        for key, value in (config.get("headers") or {}).items()
        if value is not None
    }
    for header, env_name in (config.get("header_env") or {}).items():
        value = os.environ.get(str(env_name), "")
        if value:
            headers[str(header)] = value
    token_env = str(config.get("bearer_token_env", ""))
    if token_env and os.environ.get(token_env):
        headers["Authorization"] = f"Bearer {os.environ[token_env]}"
    return headers


def _build_backend(config: Mapping[str, Any]) -> TextStateBackend:
    backend_name = str(config.get("backend", "")).strip()
    if backend_name == "mcp":
        mcp = config.get("mcp", {})
        if not isinstance(mcp, dict):
            raise ExternalStateError("external_state.mcp must be a mapping")
        url = str(mcp.get("url", ""))
        url_env = str(mcp.get("url_env", ""))
        if url_env:
            url = os.environ.get(url_env, url)
        return McpTextStateBackend(
            url,
            headers=_resolve_headers(mcp),
            timeout_seconds=float(mcp.get("timeout_seconds", 20)),
        )
    if ":" in backend_name:
        module_name, factory_name = backend_name.split(":", 1)
        factory = getattr(importlib.import_module(module_name), factory_name)
        backend = factory(config)
        if not isinstance(backend, TextStateBackend):
            raise ExternalStateError(
                f"external state factory {backend_name} did not return TextStateBackend"
            )
        return backend
    raise ExternalStateError(
        "external_state.backend must be 'mcp' or a 'module:factory' reference"
    )


def _safe_relative_key(key: str) -> Path:
    path = PurePosixPath(key)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ExternalStateError(f"unsafe external-state object key: {key!r}")
    return Path(*path.parts)


class ExternalStateRuntime:
    """Hydrates and synchronizes filesystem compatibility caches."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.strict = bool(config.get("strict", True))
        self.backend = _build_backend(config)
        identity = hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        self.root = Path(tempfile.mkdtemp(prefix=f"hermes-state-{identity}-"))
        self.memory_dir = self.root / "memories"
        self.skills_dir = self.root / "skills"
        self._hydrated: set[str] = set()
        self._etags: Dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        atexit.register(shutil.rmtree, self.root, True)

    def _section(self, name: str) -> Mapping[str, Any]:
        value = self.config.get(name, {})
        return value if isinstance(value, dict) else {}

    def enabled(self, name: str) -> bool:
        return bool(self._section(name).get("enabled", False))

    def namespace(self, name: str) -> str:
        default = "hermes-memory" if name == "memory" else "hermes-skills"
        return str(self._section(name).get("namespace", default))

    def _hydrate(self, name: str, target: Path) -> Path:
        if name in self._hydrated:
            return target
        with self._lock:
            if name in self._hydrated:
                return target
            namespace = self.namespace(name)
            target.mkdir(parents=True, exist_ok=True)
            try:
                objects = self.backend.list(namespace)
                for listed in objects:
                    relative = _safe_relative_key(listed.key)
                    value = self.backend.get(namespace, listed.key)
                    if value is None:
                        continue
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(value.content, encoding="utf-8")
                    self._etags[(namespace, listed.key)] = value.etag
            except Exception:
                if self.strict:
                    raise
                logger.warning(
                    "External %s state hydration failed; using ephemeral cache",
                    name,
                    exc_info=True,
                )
            self._hydrated.add(name)
            return target

    def get_memory_dir(self) -> Path:
        return self._hydrate("memory", self.memory_dir)

    def get_skills_dir(self) -> Path:
        return self._hydrate("skills", self.skills_dir)

    def sync_file(self, name: str, path: Path) -> None:
        root = self.get_memory_dir() if name == "memory" else self.get_skills_dir()
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        namespace = self.namespace(name)
        content = path.read_text(encoding="utf-8")
        current = self._etags.get((namespace, relative), "")
        try:
            value = self.backend.put(
                namespace,
                relative,
                content,
                content_type="text/markdown; charset=utf-8"
                if path.suffix.lower() == ".md"
                else "text/plain; charset=utf-8",
                expected_etag=current,
            )
        except Exception:
            # Another process may have advanced the object after this cache was
            # hydrated. Restore the authoritative value before surfacing the
            # conflict so a caller that retries cannot overwrite from the
            # rejected local mutation.
            authoritative = self.backend.get(namespace, relative)
            if authoritative is None:
                path.unlink(missing_ok=True)
                self._etags.pop((namespace, relative), None)
            else:
                path.write_text(authoritative.content, encoding="utf-8")
                self._etags[(namespace, relative)] = authoritative.etag
            raise
        self._etags[(namespace, relative)] = value.etag

    def sync_tree(self, name: str, root: Path) -> None:
        authoritative_root = (
            self.get_memory_dir() if name == "memory" else self.get_skills_dir()
        )
        root.resolve().relative_to(authoritative_root.resolve())
        namespace = self.namespace(name)
        local_keys: set[str] = set()
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.resolve().relative_to(authoritative_root.resolve()).as_posix()
                local_keys.add(relative)
                self.sync_file(name, path)
        prefix = root.resolve().relative_to(authoritative_root.resolve()).as_posix()
        if prefix == ".":
            prefix = ""
        elif prefix:
            prefix += "/"
        remote = self.backend.list(namespace, prefix)
        for item in remote:
            if item.key not in local_keys:
                expected = self._etags.get((namespace, item.key), item.etag)
                self.backend.delete(namespace, item.key, expected_etag=expected)
                self._etags.pop((namespace, item.key), None)


_runtime_lock = threading.Lock()
_runtimes: Dict[str, ExternalStateRuntime] = {}


def get_external_state_runtime() -> Optional[ExternalStateRuntime]:
    """Return the configured runtime, or ``None`` when external state is off."""

    config = _load_config()
    if not str(config.get("backend", "")).strip():
        return None
    from hermes_constants import get_hermes_home

    # Dashboard/TUI processes can host multiple profiles. Never let one
    # profile's seat, cache, or backend leak into another merely because both
    # profiles share an interpreter. Include referenced environment values in
    # the hashed identity so endpoint, seat, or credential rotation does not
    # retain a client built with stale connection state.
    mcp_config = config.get("mcp", {})
    env_names: set[str] = set()
    if isinstance(mcp_config, dict):
        for value in (
            mcp_config.get("url_env"),
            mcp_config.get("bearer_token_env"),
        ):
            if value:
                env_names.add(str(value))
        header_env = mcp_config.get("header_env", {})
        if isinstance(header_env, dict):
            env_names.update(str(value) for value in header_env.values() if value)
    identity = json.dumps(
        {
            "home": str(get_hermes_home()),
            "config": config,
            "environment": {name: os.environ.get(name, "") for name in env_names},
        },
        sort_keys=True,
        default=str,
    )
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    runtime = _runtimes.get(key)
    if runtime is None:
        with _runtime_lock:
            runtime = _runtimes.get(key)
            if runtime is None:
                runtime = ExternalStateRuntime(config)
                _runtimes[key] = runtime
    return runtime


def reset_external_state_runtime_for_tests() -> None:
    with _runtime_lock:
        for runtime in _runtimes.values():
            shutil.rmtree(runtime.root, ignore_errors=True)
        _runtimes.clear()
