"""``HonchoMemoryProvider.identity_signature()``: the identity-mapping values the gateway folds
into its agent-cache key, read from honcho.json without touching the network."""

import json
import os
from pathlib import Path

import pytest

from plugins.memory.honcho import HonchoMemoryProvider


@pytest.fixture
def honcho_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "honcho.json"

    def _write(**values):
        path.write_text(json.dumps({"apiKey": "k", **values}))
        return path

    return _write


def test_signature_uses_neutral_keys(honcho_json):
    honcho_json(workspace="team", peerName="eri", aiPeer="hermes", pinUserPeer=True, runtimePeerPrefix="tg_",
                userPeerAliases={"222": "bob", "111": "alice"}, sessionPeerPrefix=True, a2aSessions=False)

    sig = HonchoMemoryProvider().identity_signature()

    assert sig == {
        "workspace": "team",
        "user_identity": "eri",
        "agent_identity": "hermes",
        "pin_user_identity": True,
        "runtime_identity_prefix": "tg_",
        "user_identity_aliases": [("111", "alice"), ("222", "bob")],
        "session_prefixing": [True, False],
        "a2a_sessions": False,
    }
    assert not any(k.startswith("honcho") for k in sig)


def test_signature_tracks_edits_to_the_file(honcho_json):
    provider = HonchoMemoryProvider()
    honcho_json(peerName="eri", pinUserPeer=True)
    assert provider.identity_signature()["pin_user_identity"] is True

    honcho_json(peerName="eri", pinUserPeer=False)
    assert provider.identity_signature()["pin_user_identity"] is False


def test_signature_never_touches_the_network(honcho_json, network_attempts):
    honcho_json(peerName="eri")
    HonchoMemoryProvider().identity_signature()
    assert network_attempts == []


def test_signature_tracks_replacement_preserving_mtime_and_size(honcho_json):
    from unittest.mock import patch

    from plugins.memory.honcho.client import HonchoClientConfig

    path = honcho_json(peerName="eri")
    before = path.stat()
    provider = HonchoMemoryProvider()
    with patch.object(
        HonchoClientConfig, "from_global_config", wraps=HonchoClientConfig.from_global_config
    ) as load:
        first = provider.identity_signature()
        assert first["user_identity"] == "eri"
        assert provider.identity_signature() == first
        assert load.call_count == 1  # An unchanged file still uses the memoized identity.

        replacement = path.with_suffix(".replacement")
        replacement.write_text(json.dumps({"apiKey": "k", "peerName": "ada"}))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)

        assert provider.identity_signature()["user_identity"] == "ada"
        assert load.call_count == 2


def test_signature_remains_profile_scoped_across_a_b_a(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    for home, peer in ((home_a, "eri"), (home_b, "ada")):
        home.mkdir()
        (home / "honcho.json").write_text(json.dumps({"apiKey": "k", "peerName": peer}))
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    provider = HonchoMemoryProvider()
    was_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        for home, peer in ((home_a, "eri"), (home_b, "ada"), (home_a, "eri")):
            home_token = set_hermes_home_override(home)
            secret_token = secret_scope.set_secret_scope({}, profile_home=str(home))
            try:
                assert provider.identity_signature()["user_identity"] == peer
            finally:
                secret_scope.reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
    finally:
        secret_scope.set_multiplex_active(was_multiplex)
