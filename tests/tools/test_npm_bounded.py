"""Behavior contracts for the finite npm-install wrapper used by Docker builds."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_helper(tmp_path: Path, exit_code: int) -> tuple[subprocess.CompletedProcess, list[str], list[str]]:
    fake_npm = tmp_path / "npm"
    fake_npm.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "$NPM_ARGS"\nexit {exit_code}\n')
    fake_npm.chmod(0o755)
    fake_timeout = tmp_path / "timeout"
    fake_timeout.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TIMEOUT_ARGS"\nshift 3\nexec "$@"\n')
    fake_timeout.chmod(0o755)
    npm_args, timeout_args = tmp_path / "npm.args", tmp_path / "timeout.args"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "NPM_ARGS": str(npm_args), "TIMEOUT_ARGS": str(timeout_args),
           "NPM_BOUNDED_ATTEMPTS": "1", "NPM_BOUNDED_TIMEOUT_SECONDS": "5"}
    result = subprocess.run(["/bin/sh", str(REPO_ROOT / "docker/npm-bounded.sh"), "install"],
                            env=env, check=False, capture_output=True, text=True)
    return result, npm_args.read_text().splitlines(), timeout_args.read_text().splitlines()


def test_bounded_npm_helper_passes_limits_and_original_command(tmp_path: Path) -> None:
    result, npm, timeout = _run_helper(tmp_path, 0)
    assert result.returncode == 0
    assert timeout[:3] == ["--signal=TERM", "--kill-after=15s", "5s"]
    assert timeout[3] == "npm"
    assert npm == ["--fetch-retries=3", "--fetch-retry-mintimeout=1000",
                   "--fetch-retry-maxtimeout=5000", "--fetch-timeout=30000", "install"]


def test_bounded_npm_helper_preserves_failure_status(tmp_path: Path) -> None:
    result, _, _ = _run_helper(tmp_path, 42)
    assert result.returncode == 42
    assert "status 42" in result.stderr
