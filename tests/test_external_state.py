from pathlib import Path

import external_state


class FakeBackend(external_state.TextStateBackend):
    def __init__(self):
        self.values = {}

    def get(self, namespace, key):
        value = self.values.get((namespace, key))
        if value is None:
            return None
        return external_state.StateObject(key=key, content=value, etag=str(hash(value)))

    def list(self, namespace, prefix=""):
        return [
            external_state.StateObject(key=key, content="", etag=str(hash(value)))
            for (stored_namespace, key), value in self.values.items()
            if stored_namespace == namespace and key.startswith(prefix)
        ]

    def put(
        self,
        namespace,
        key,
        content,
        *,
        content_type="text/plain; charset=utf-8",
        expected_etag=None,
    ):
        current = self.get(namespace, key)
        actual = current.etag if current else ""
        if expected_etag is not None and expected_etag != actual:
            raise external_state.ExternalStateError("etag conflict")
        self.values[(namespace, key)] = content
        return self.get(namespace, key)

    def delete(self, namespace, key, *, expected_etag=None):
        current = self.get(namespace, key)
        actual = current.etag if current else ""
        if expected_etag is not None and expected_etag != actual:
            raise external_state.ExternalStateError("etag conflict")
        return self.values.pop((namespace, key), None) is not None


def runtime(backend):
    instance = object.__new__(external_state.ExternalStateRuntime)
    instance.config = {
        "strict": True,
        "memory": {"enabled": True, "namespace": "memory"},
        "skills": {"enabled": True, "namespace": "skills"},
    }
    instance.strict = True
    instance.backend = backend
    instance.root = Path(external_state.tempfile.mkdtemp(prefix="hermes-state-test-"))
    instance.memory_dir = instance.root / "memories"
    instance.skills_dir = instance.root / "skills"
    instance._hydrated = set()
    instance._etags = {}
    instance._lock = external_state.threading.RLock()
    return instance


def test_memory_hydrates_and_persists_without_profile_files():
    backend = FakeBackend()
    backend.values[("memory", "MEMORY.md")] = "first"
    state = runtime(backend)

    path = state.get_memory_dir() / "MEMORY.md"
    assert path.read_text() == "first"
    path.write_text("second")
    state.sync_file("memory", path)
    assert backend.values[("memory", "MEMORY.md")] == "second"


def test_skill_tree_sync_removes_deleted_remote_objects():
    backend = FakeBackend()
    backend.values[("skills", "old/SKILL.md")] = "old"
    state = runtime(backend)
    root = state.get_skills_dir()
    (root / "old" / "SKILL.md").unlink()
    (root / "old").rmdir()
    (root / "new").mkdir()
    (root / "new" / "SKILL.md").write_text("new")

    state.sync_tree("skills", root)

    assert ("skills", "old/SKILL.md") not in backend.values
    assert backend.values[("skills", "new/SKILL.md")] == "new"


def test_write_conflict_restores_authoritative_cache():
    backend = FakeBackend()
    backend.values[("memory", "MEMORY.md")] = "first"
    state = runtime(backend)
    path = state.get_memory_dir() / "MEMORY.md"

    backend.values[("memory", "MEMORY.md")] = "concurrent"
    path.write_text("stale mutation")
    try:
        state.sync_file("memory", path)
    except external_state.ExternalStateError as exc:
        assert "conflict" in str(exc)
    else:
        raise AssertionError("concurrent write was not rejected")

    assert path.read_text() == "concurrent"


def test_unsafe_object_key_is_rejected():
    backend = FakeBackend()
    backend.values[("skills", "../escape")] = "bad"
    state = runtime(backend)

    try:
        state.get_skills_dir()
    except external_state.ExternalStateError as exc:
        assert "unsafe" in str(exc)
    else:
        raise AssertionError("unsafe key was accepted")
