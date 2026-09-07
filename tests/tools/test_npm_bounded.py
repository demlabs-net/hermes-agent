"""Contract tests for finite network installs in Docker builds."""
from __future__ import annotations

from pathlib import Path


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
