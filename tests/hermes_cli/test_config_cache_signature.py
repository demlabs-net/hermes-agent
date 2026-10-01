"""#111105: a config.yaml replacement that keeps mtime and size (``cp -p``, ``rsync -t``, a
timestamp-pinning writer) must still invalidate the load_config() cache, while an unchanged
file keeps serving the cached object."""
import os
import shutil
from unittest.mock import patch

import pytest

from hermes_cli import config as config_mod


def _replace_pinning_mtime(path, content: str) -> None:
    before = path.stat()
    other = path.with_name("other.yaml")
    other.write_text(content, encoding="utf-8")
    shutil.copy2(other, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("reader", [config_mod.load_config_readonly, config_mod.read_raw_config_readonly])
def test_load_config_sees_replacement_with_pinned_mtime_and_size(tmp_path, reader):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        config_mod.invalidate_config_cache()
        cfg = tmp_path / "config.yaml"
        cfg.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
        first = reader()
        assert reader() is first  # unchanged file: cache hit
        _replace_pinning_mtime(cfg, "model:\n  default: bbbb-route\n")
        assert reader()["model"]["default"] == "bbbb-route"


def test_managed_replacement_invalidates_both_managed_and_merged_caches(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    home, managed = tmp_path / "home", tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    config_mod.invalidate_config_cache()
    managed_scope.invalidate_managed_cache()
    (home / "config.yaml").write_text("model:\n  default: user-route\n", encoding="utf-8")
    cfg = managed / "config.yaml"
    cfg.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
    first = config_mod.load_config_readonly()
    assert first["model"]["default"] == "aaaa-route"
    assert config_mod.load_config_readonly() is first

    _replace_pinning_mtime(cfg, "model:\n  default: bbbb-route\n")

    assert managed_scope.load_managed_config()["model"]["default"] == "bbbb-route"
    assert config_mod.load_config()["model"]["default"] == "bbbb-route"
    assert config_mod.read_raw_config()["model"]["default"] == "user-route"


def test_explicit_reset_reloads_raw_and_merged_even_with_unchanged_stat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod.invalidate_config_cache()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
    signature = config_mod._file_cache_signature(cfg.stat())
    monkeypatch.setattr(config_mod, "_file_cache_signature", lambda _stat: signature)
    readers = (config_mod.load_config_readonly, config_mod.read_raw_config_readonly)
    for reader in readers:
        assert reader()["model"]["default"] == "aaaa-route"

    cfg.write_text("model:\n  default: bbbb-route\n", encoding="utf-8")
    config_mod.invalidate_config_cache(cfg)

    for reader in readers:
        assert reader()["model"]["default"] == "bbbb-route"
