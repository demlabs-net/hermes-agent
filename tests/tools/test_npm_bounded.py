"""Contract tests for finite network installs in Docker builds."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bounded_npm_helper_has_timeout_retry_and_fetch_limits() -> None:
    helper = (REPO_ROOT / "docker" / "npm-bounded.sh").read_text()

    assert "NPM_BOUNDED_ATTEMPTS:-3" in helper
    assert "NPM_BOUNDED_TIMEOUT_SECONDS:-300" in helper
    assert "timeout --signal=TERM" in helper
    assert "--fetch-retries=3" in helper
    assert "--fetch-timeout=30000" in helper


def test_base_image_uses_bounded_network_installs() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert "docker/npm-bounded.sh /usr/local/bin/npm-bounded" in dockerfile
    assert "npm-bounded install --prefer-offline --no-audit" in dockerfile
    assert "NPM_BOUNDED_TIMEOUT_SECONDS=180 npm-bounded ci --no-audit" in dockerfile
    assert "timeout --signal=TERM --kill-after=15s 300s" in dockerfile


def test_bounded_npm_helper_preserves_failure_status(tmp_path: Path) -> None:
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("#!/bin/sh\nexit 42\n")
    fake_npm.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "NPM_BOUNDED_ATTEMPTS": "1",
            "NPM_BOUNDED_TIMEOUT_SECONDS": "5",
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(REPO_ROOT / "docker" / "npm-bounded.sh"), "install"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    assert "status 42" in result.stderr
