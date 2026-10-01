# Hermes upstream integration — 2026-10-01

## Source and isolation

- Fork: `git@gitlab.demlabs.net:internal/ai/hermes.git`.
- Original `main` / `origin/main`: `315767d57581b2df2d3307a7d2ec71ada20dc663`.
- Official stable release: **Hermes0.21.5**, tag `v2026.9.24`.
- Upstream commit: `f97608f178d1ffeca59860195ab7da295f7c8e5f`.
- Merge base: `95cfcba353a1f8955dee38e5005f51981e8ff05f`.
- Divergence: 42 fork-side commits, 8294 upstream-side commits.
- Integration branch: `integration/upstream-v2026.9.24-20261001`.
- Working directory: `/Users/dmitriygerasimov/work/ai/agents/hermes`.
- The integration and its merge state were transferred into this original
  checkout at the operator's request. The extra worktree was removed.
- Publication branch: `integration/upstream-v2026.9.24-20261001`.
  The operator explicitly requested committing and pushing all integration changes.
  The merge commit preserves both parent histories; `main` is not overwritten.

This report accompanies the integration merge. Publishing this branch does not
upgrade or deploy containers. Existing Atlant agents remain on their previously
running images.

## Resolution

41 conflicted index paths resolved. Upstream history and fork history retained;
no wholesale ours/theirs conflict resolution. Updated metadata reports0.21.5.
Upstream dependency manifests/lockfiles remain unchanged by conflict resolution.

Preserved DemLabs behavior:
- operator hold, generation revocation, physical-dispatch barriers and cron authorizations;
- stop-before-worker-start semantics, run deadlines, orphan cleanup and process reaping;
- strict external memory/skill persistence and exact tool-call recovery;
- MCP application errors remain distinct from transport circuit-breaker failures;
- custom image routing, role-scoped credentials and bounded npm network installs;
- SLC hooks, swarm_messaging plugin, VoIP/media and existing custom skills.

New upstream peer paths inherit operator generations through mailbox delivery and
reject stale grants before execution. Asynchronous live-owner admission is excluded
from orphan sweeping while its worker is being handed off.

Integration fixes include excluding process-local `.locks` from external skill
persistence, aligning external skill readers with writers, retaining stronger
config/Honcho file identities and applying hold checks to new dispatch/retry paths.

## Tests and environment

Created an isolated Python3.13 venv, installed declared dev/all/messaging/anthropic
extras, and used **scripts/run_tests.sh** (clean env, temporary HERMES_HOME,
per-file subprocesses, no retry masking). The environment was moved to
`hermes/.venv` and its entry points reinstalled for that path. The pre-existing
Python3.11 environment is preserved in `hermes/.venv-python3.11` and locally
excluded from Git. Post-move `pip check` passes. Editable imports resolve to
`/Users/dmitriygerasimov/work/ai/agents/hermes`, including from an unrelated cwd.

Post-move verification: 15105 tracked paths matched byte-for-byte before removing
the extra worktree; index tree, unstaged patch and merge metadata matched exactly.
Only the root checkout remains. The canonical test runner was then run from
`hermes`: **277 passed, 0 failed** across nine API/peer/MCP/operator-hold/skills/
external-state/packaging test files. All conflicts were resolved before commit;
the staged/unstaged distinction was preserved during relocation, then the complete
reviewed integration was staged for the operator-requested merge commit.

Focused groups:
- Core/transport/hold/external state: 474 passes initially, two hold-order failures
  subsequently fixed and passed in gateway/recheck groups.
- Config/migration/peer CLI: 394 passes.
- MCP/tools/memory/skills/peer mailbox: 550 passes, 17 skips.
- Gateway/native peer/hold: 379 passes.
- Extra reconnect/cron/fork regressions: 165 passes initially; three fixture/test
  integration errors subsequently fixed and verified.
- Corrected packaging/npm/media/peer group: 122 passes.

Broad regression:
- 4143 test files in agent, gateway, tools, hermes_cli, hermes_state, honcho_plugin,
  cron plus selected root tests.
- Initial run: **42762 passed, 60 failed, 637 skipped**, about20minutes.
- All21 failing files were diagnosed and rerun after fixes/environment correction:
  **608 passed, 0 failed, 56 skipped**. This is a targeted final rerun, not a second
  complete broad run.

Most broad failures were upstream portability assumptions on macOS. Corrections:
- Local `/var/tmp` aliases must preserve the same frozen workspace prompt; genuine
  upstream cache-key bug fixed with red-before/green-after behavior regression.
- Optional missing/inaccessible `gh` should not crash doctor: catch OSError.
- Host-specific systemd/WSL/virtiofs/desktop tests use platform markers, not fake OS.
- Tests compare file identity/canonical paths and use portable process counting;
  Linux/macOS synchronous durability behavior is tested separately.
- Short Unix-socket paths and complete mocked audio prerequisites avoid real devices.
- Two git tests require a newer Git: final runs used Apple Git2.39.5 instead of
  Homebrew Git2.36.0. No application security guard was disabled for tests.

All tracked Python sources parse; no conflict markers/unmerged entries remain.
Integration diff against upstream passes `git diff --check`. Upstream itself has
whitespace diagnostics relative to old fork HEAD; unrelated upstream formatting
was not rewritten. Static undefined-name check has no findings outside the
intentional dynamically bound TUI sibling.

Independent bounded review found no confirmed new P1 in inspected safety paths;
its external-skill-reader consistency observation was fixed and covered.

Not performed: full JS/desktop/TUI build, Docker image build, Linux/Windows-only
runtime tests, production calls to providers/SLC, upgrade of existing containers.
Those remain release/deployment validation, not implied by Python test success.

## Existing WIP restriction

Original fork HEAD is titled:
`wip: admission-first workflow runs (draft, disabled, never deployed)`.
Its commit message explicitly says it is incomplete and must not be deployed as-is.
The integration preserves it **disabled**, including empty protected-profile list;
it neither drops operator work nor silently declares it production-ready.

Therefore this branch is an upstream integration result, **not an approved
production image**. A deployment candidate must deliberately address the existing
WIP restriction and pass image/runtime acceptance before replacing working agents.

## Prior analytics task status

Credential-free crypto analytics modules and optional analyst profile were prepared
in the separate `atlant-agents` directory before this repository task. They were
not deployed during this integration. Native peer on the current0.20.0 containers
must not be described as available;0.21.5 peer behavior is tested here locally,
not yet running on Atlant. Dedicated analyst inference key is still required.
