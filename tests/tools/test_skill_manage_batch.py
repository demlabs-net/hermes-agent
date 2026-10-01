"""skill_manage operations[] batch (#95681 arc, maintainer-approved).

Memory-tool pattern: several ops on ONE skill, atomically — create + N
supporting files, or SKILL.md + the script it references, in one call.
Any failure rolls the skill directory back to its pre-batch state.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SK = (
    "---\nname: {n}\ndescription: Use when probing batch ops. Behavior.\n---\n"
    "# Probe\nStep 1.\n"
)


class TestSkillManageBatch(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="skmbatch_t_")
        os.environ["HERMES_HOME"] = self.home
        os.environ["HERMES_YOLO_MODE"] = "1"
        os.makedirs(os.path.join(self.home, "skills"), exist_ok=True)
        # Re-import against the temp home (module caches SKILLS_DIR).
        import importlib

        import tools.skill_manager_tool as smt
        importlib.reload(smt)
        self.smt = smt

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _call(self, name, ops):
        # Inject the per-op name (tests were written per-skill; the
        # interface is name-per-op, maintainer-directed).
        for op in ops:
            op.setdefault("name", name)
        return json.loads(self.smt.skill_manage(action="", name="", operations=ops))

    def test_create_plus_files_atomic(self):
        r = self._call("probe", [
            {"action": "create", "content": SK.format(n="probe")},
            {"action": "write_file", "file_path": "references/a.md", "file_content": "a"},
            {"action": "write_file", "file_path": "scripts/r.py", "file_content": "pass"},
        ])
        self.assertTrue(r["success"], r)
        self.assertEqual(r["operations_applied"], 3)
        base = os.path.join(self.home, "skills", "probe")
        for rel in ("SKILL.md", "references/a.md", "scripts/r.py"):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)

    def test_advisory_findings_ride_on_the_op_result(self):
        # operations[] is the only call shape, so a finding the per-op handler attaches must
        # survive into the batch row or the linter is silent for every model call.
        from tools.skill_linter import _BODY_SOFT_BUDGET_CHARS
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [{"action": "patch", "old_string": "Step 1.",
                                  "new_string": "- rule; why.\n" * (_BODY_SOFT_BUDGET_CHARS // 12 + 1)}])
        self.assertTrue(r["success"], r)
        rules = {w["rule"] for w in r["results"][0]["lint_warnings"]}
        self.assertIn("oversized-body", rules)

    def test_midbatch_failure_rolls_back_existing_skill(self):
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "Step ONE."},
            {"action": "write_file", "file_path": "bad/nope.md", "file_content": "x"},
        ])
        self.assertFalse(r["success"])
        self.assertEqual(r["failed_index"], 1)
        content = open(os.path.join(self.home, "skills", "probe", "SKILL.md")).read()
        self.assertIn("Step 1.", content)       # patch undone
        self.assertNotIn("Step ONE.", content)

    def test_failed_create_batch_removes_partial_skill(self):
        r = self._call("fresh", [
            {"action": "create", "content": SK.format(n="fresh")},
            {"action": "write_file", "file_path": "../escape.md", "file_content": "x"},
        ])
        self.assertFalse(r["success"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "fresh")))

    def test_validation_rules(self):
        # delete as SOLE op routes to the real delete (works)
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [{"action": "delete"}])
        self.assertTrue(r["success"], r)
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "probe")))
        # delete mixed with other ops rejected
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "X."},
            {"action": "delete"},
        ])
        self.assertFalse(r["success"])
        self.assertIn("SOLE", r["error"])
        # create must be first
        r = self._call("x", [
            {"action": "write_file", "file_path": "references/a.md", "file_content": "a"},
            {"action": "create", "content": SK.format(n="x")},
        ])
        self.assertFalse(r["success"])
        # empty / capped
        r = self._call("x", [])
        self.assertFalse(r["success"])
        r = self._call("x", [{"action": "patch"}] * 21)
        self.assertFalse(r["success"])
        self.assertIn("capped", r["error"])

    def test_intra_batch_conflict_guard(self):
        """Same-file double writes and post-edit full rewrites are always
        a confused plan under last-wins sequencing — rejected BEFORE any
        side effect. Patch chains and rewrite-first stay legal."""
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        # destructive op on an already-touched file: rejected — double
        # write, write+remove, patch-then-write, patch-then-remove, and a
        # path-spelling variant of the same file.
        self._call("probe", [{"action": "write_file",
                              "file_path": "references/c.md", "file_content": "seed"}])
        for ops in (
            [{"action": "write_file", "file_path": "references/a.md", "file_content": "1"},
             {"action": "write_file", "file_path": "references/a.md", "file_content": "2"}],
            [{"action": "write_file", "file_path": "references/b.md", "file_content": "x"},
             {"action": "remove_file", "file_path": "references/b.md"}],
            [{"action": "patch", "file_path": "references/c.md",
              "old_string": "seed", "new_string": "edited"},
             {"action": "write_file", "file_path": "references/c.md", "file_content": "CLOB"}],
            [{"action": "patch", "file_path": "references/c.md",
              "old_string": "seed", "new_string": "edited"},
             {"action": "remove_file", "file_path": "references/c.md"}],
            [{"action": "write_file", "file_path": "references/d.md", "file_content": "1"},
             {"action": "write_file", "file_path": "./references//d.md", "file_content": "2"}],
        ):
            r = self._call("probe", ops)
            self.assertFalse(r["success"], ops)
            self.assertIn("discard", r["error"])
        # ...and rejected pre-effect: c.md still holds its seed text.
        c_md = os.path.join(self.home, "skills", "probe", "references", "c.md")
        self.assertEqual(open(c_md).read(), "seed")
        # write-then-patch on one supporting file stays legal (additive).
        r = self._call("probe", [
            {"action": "write_file", "file_path": "references/e.md", "file_content": "base"},
            {"action": "patch", "file_path": "references/e.md",
             "old_string": "base", "new_string": "base+"},
        ])
        self.assertTrue(r["success"], r)
        # patch then full rewrite: rejected; rewrite-first: allowed
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "P."},
            {"action": "patch", "content": SK.format(n="probe")},
        ])
        self.assertFalse(r["success"])
        self.assertIn("rewrite", r["error"])
        r = self._call("probe", [
            {"action": "patch", "content": SK.format(n="probe").replace("Step 1.", "F.")},
            {"action": "patch", "old_string": "F.", "new_string": "G."},
        ])
        self.assertTrue(r["success"], r)
        # patch chains stay legal
        r = self._call("probe", [
            {"action": "patch", "old_string": "G.", "new_string": "H."},
            {"action": "patch", "old_string": "H.", "new_string": "I."},
        ])
        self.assertTrue(r["success"], r)

    def test_cross_skill_batch_and_rollback(self):
        """Ops may target DIFFERENT skills; a late failure rolls back
        every touched skill, including removing a batch-created one."""
        self._call("alpha", [{"action": "create", "content": SK.format(n="alpha")}])
        r = json.loads(self.smt.skill_manage(action="", name="", operations=[
            {"name": "alpha", "action": "patch",
             "old_string": "Step 1.", "new_string": "Step A."},
            {"name": "beta", "action": "create", "content": SK.format(n="beta")},
            {"name": "beta", "action": "write_file",
             "file_path": "bad/nope.md", "file_content": "x"},
        ]))
        self.assertFalse(r["success"])
        self.assertEqual(r["failed_index"], 2)
        # alpha's patch undone; beta (batch-created) removed entirely.
        content = open(os.path.join(self.home, "skills", "alpha", "SKILL.md")).read()
        self.assertIn("Step 1.", content)
        self.assertNotIn("Step A.", content)
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "beta")))

    def test_failed_restore_never_destroys_the_skill(self):
        """Rollback used to rmtree the live skill directory BEFORE
        copytree restored the snapshot. When copytree failed (disk full,
        locked file) the except only appended a note and the finally then
        deleted the snapshot too: nothing survived. The broken state must
        be moved aside and only deleted once the restore succeeded."""
        from unittest.mock import patch as _patch

        import shutil as _shutil

        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        state = {"n": 0}
        real_copytree = _shutil.copytree

        def flaky_copytree(src, dst, *a, **k):
            state["n"] += 1
            if state["n"] == 2:  # call 1 snapshots, call 2 is the restore
                raise OSError("disk full")
            return real_copytree(src, dst, *a, **k)

        with _patch("shutil.copytree", side_effect=flaky_copytree):
            r = self._call("probe", [
                {"action": "patch",
                 "old_string": "Step 1.", "new_string": "Step ONE."},
                {"action": "write_file",
                 "file_path": "bad/nope.md", "file_content": "x"},
            ])
        self.assertFalse(r["success"], r)
        self.assertIn("ROLLBACK FAILED", r["error"])
        # The skill directory was NOT destroyed by the failed rollback:
        # the half applied state survives instead of nothing at all.
        skill_md = os.path.join(self.home, "skills", "probe", "SKILL.md")
        self.assertTrue(os.path.exists(skill_md))
        content = open(skill_md).read()
        self.assertIn("Step ONE.", content)

    def test_single_op_path_unchanged(self):
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        raw = self.smt.skill_manage(
            action="patch", name="probe",
            old_string="Step 1.", new_string="Step 1 (single).",
        )
        self.assertTrue(json.loads(raw)["success"])

    def test_batch_stages_as_one_pending_write_when_gated(self):
        """Approval gate: the whole batch stages as ONE pending record, and
        apply_skill_pending replays it (operations key round-trips)."""
        from unittest.mock import patch as _patch

        class _Decision:
            allow = False
            blocked = False
            message = "staged for review"

        staged = {}

        def fake_stage_write(area, payload, summary=None, origin=None):
            staged.update(payload=payload, summary=summary)
            return {"id": "pend_1"}

        import tools.write_approval as wa

        with _patch.object(wa, "evaluate_gate", return_value=_Decision()), \
             _patch.object(wa, "stage_write", side_effect=fake_stage_write):
            r = self._call("probe", [
                {"action": "create", "content": SK.format(n="probe")},
                {"action": "write_file", "file_path": "references/a.md",
                 "file_content": "a"},
            ])
        self.assertTrue(r.get("staged"), r)
        self.assertEqual(staged["payload"]["action"], "batch")
        self.assertEqual(len(staged["payload"]["operations"]), 2)
        # Replay applies the batch (gate bypassed inside).
        out = json.loads(self.smt.apply_skill_pending(staged["payload"]))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["operations_applied"], 2)


def _external_skill_runtime(tmp_path, monkeypatch):
    """Real compatibility cache and sync path, with an in-memory text backend."""
    import external_state
    import tools.skill_manager_tool as smt
    class Backend(external_state.TextStateBackend):
        def __init__(self):
            self.values = {}

        def get(self, namespace, key):
            content = self.values.get((namespace, key))
            return None if content is None else external_state.StateObject(key, content, etag=str(hash(content)))

        def list(self, namespace, prefix=""):
            return [self.get(ns, key) for ns, key in self.values if ns == namespace and key.startswith(prefix)]

        def put(self, namespace, key, content, *, content_type="text/plain", expected_etag=None):
            current = self.get(namespace, key)
            if expected_etag is not None and expected_etag != (current.etag if current else ""):
                raise external_state.ExternalStateError("etag conflict")
            self.values[(namespace, key)] = content
            return self.get(namespace, key)

        def delete(self, namespace, key, *, expected_etag=None):
            return self.values.pop((namespace, key), None) is not None

    backend = Backend()
    monkeypatch.setattr(external_state, "_build_backend", lambda config: backend)
    runtime = external_state.ExternalStateRuntime({
        "skills": {"enabled": True, "namespace": "skills"}, "strict": True,
    })
    monkeypatch.setattr(external_state, "get_external_state_runtime", lambda: runtime)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr(smt, "SKILLS_DIR", runtime.get_skills_dir())
    return smt, runtime, backend


def test_external_batch_publishes_only_after_all_ops_succeed(tmp_path, monkeypatch):
    from tools.registry import registry

    smt, runtime, backend = _external_skill_runtime(tmp_path, monkeypatch)
    content = SK.format(n="probe")
    # The advertised operations[] shape goes through the actual registry handler.
    def dispatch(ops):
        return json.loads(registry.dispatch("skill_manage", {"operations": ops}))

    created = dispatch([{"name": "probe", "action": "create", "content": content}])
    assert created["success"], created
    before = dict(backend.values)
    calls = []
    original_put = backend.put

    def record_put(*args, **kwargs):
        calls.append(args)
        return original_put(*args, **kwargs)

    monkeypatch.setattr(backend, "put", record_put)
    failed = dispatch([
        {"name": "probe", "action": "patch", "old_string": "Step 1.", "new_string": "Step 2."},
        {"name": "probe", "action": "write_file", "file_path": "../escape", "file_content": "x"},
    ])
    assert not failed["success"]
    assert not calls, "a rolled-back op must never escape to the external backend"
    assert backend.values == before
    assert (runtime.get_skills_dir() / "probe" / "SKILL.md").read_text() == content

    saved = dispatch([
        {"name": "probe", "action": "patch", "old_string": "Step 1.", "new_string": "Step 2."},
        {"name": "probe", "action": "write_file", "file_path": "references/a.md", "file_content": "reference"},
    ])
    assert saved["success"], saved
    assert backend.values[("skills", "probe/SKILL.md")] == content.replace("Step 1.", "Step 2.")
    assert backend.values[("skills", "probe/references/a.md")] == "reference"
    assert not any(key.startswith(".locks/") for namespace, key in backend.values)


def test_external_batch_never_rolls_back_over_mounted_guidance(tmp_path, monkeypatch):
    from tools.registry import registry
    from tools import skill_manager_batch
    from unittest.mock import Mock

    smt, runtime, backend = _external_skill_runtime(tmp_path, monkeypatch)
    mounted = tmp_path / "mounted"
    skill = mounted / "guidance" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    content = SK.format(n="guidance")
    skill.write_text(content)
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [runtime.get_skills_dir(), mounted])
    monkeypatch.setattr("agent.skill_utils.get_skill_create_dir", lambda: mounted)
    rollback = Mock(wraps=skill_manager_batch._rollback)
    monkeypatch.setattr(skill_manager_batch, "_rollback", rollback)

    refused = json.loads(registry.dispatch("skill_manage", {"operations": [
        {"name": "fresh", "action": "create", "content": SK.format(n="fresh")},
        {"name": "guidance", "action": "patch", "old_string": "Step 1.", "new_string": "changed"},
    ]}))
    assert not refused["success"] and "read-only" in refused["error"]
    rollback.assert_not_called()
    assert skill.read_text() == content
    assert not (runtime.get_skills_dir() / "fresh").exists()
    assert not backend.values

    saved = json.loads(registry.dispatch("skill_manage", {"operations": [
        {"name": "fresh", "action": "create", "content": SK.format(n="fresh")},
    ]}))
    assert saved["success"], saved
    assert not (mounted / "fresh").exists()
    assert backend.values[("skills", "fresh/SKILL.md")] == SK.format(n="fresh")


if __name__ == "__main__":
    unittest.main()
