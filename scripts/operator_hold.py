#!/usr/bin/env python3
"""Operator-host-only control writer. Never mount this directory writable in agents.

Usage:
  python3 scripts/operator_hold.py /host/operator-hold hold
  python3 scripts/operator_hold.py /host/operator-hold release
  python3 scripts/operator_hold.py /host/operator-hold list \
      --hermes-home /host/hermes-home
  python3 scripts/operator_hold.py /host/operator-hold authorize-cron \
      --job-id <exact id> [--job-id <exact id> ...] --hermes-home /host/hermes-home
  python3 scripts/operator_hold.py /host/operator-hold authorize-extra \
      --job-id <exact id> (--extra-prompt <text> | --extra-prompt-file <path> |
      --include-job-extra-prompt --hermes-home /host/hermes-home)

Provision control.json before starting a configured agent (init is implicit: the
first hold/release writes the file). Directory mount into containers must be
read-only (mount the directory, not an individual inode). Every hold/release
rotates the generation; release also clears the cron authorizations and removes
the legacy active marker. An environment hold still wins.

Cron authorization binds ONE exact job id to the digest of its stored payload, so
editing that payload afterwards (prompt, schedule, model, toolsets, deliver, ...)
revokes it, and a hold/release rotation revokes every job at once. Agent-created
jobs are never authorized on their own: the digest is recorded only here, by the
operator. `list` is read-only and prints every job with its digest plus the exact
single command that authorizes the rest — take the ids from there, never from a
name or a wildcard, and only after reading what the job will do.

authorize-extra covers the OTHER half of a manual run: a per-fire extra prompt is
fresh instruction text, so an authorized job never carries it. Record the exact
text here (the file variant keeps it out of shell history) and that same text stays
admissible for that job until a hold/release rotation clears it.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

# Host-only utility: it imports the deployed Hermes source (cron jobs / digest) out of
# the image tree, so run it where that source is importable (see the runbook).
_SRC_ROOT = str(Path(__file__).resolve().parents[1])


def _extra_prompt_digest(text):
    """sha256 of one exact approved instruction text.

    Mirrors agent.operator_hold.extra_prompt_digest (the suite asserts the two agree) and is
    implemented locally so a host that carries no Hermes source tree can still record an
    approval for the exact text. The text itself never leaves the host.
    """
    import hashlib
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _load_control(directory):
    """Current control.json, or {} when it was never provisioned (read-only callers)."""
    try:
        return json.loads((directory / "control.json").read_text())
    except FileNotFoundError:
        return {}


def _authorization_state(job, control):
    """(digest|None, verdict) for *job* under *control*; digest is None when not authorizable."""
    from agent.operator_hold import cron_payload_digest
    job_id = str(job.get("id") or "")
    try:
        digest = cron_payload_digest(job)
    except ValueError as exc:
        return None, f"NOT-AUTHORIZABLE: {exc}"
    recorded = (control.get("authorized_cron_jobs") or {}).get(job_id)
    generation = control.get("generation")
    if recorded == digest and job.get("operator_generation") == generation:
        return digest, "authorized"
    if recorded == digest:
        return digest, "DENIED (stale generation: rotate authorized the job, a newer rotation revoked it)"
    if recorded:
        return digest, "DENIED (payload edited since authorization)"
    if job.get("operator_generation"):
        # hold/release clears authorized_cron_jobs and rotates the generation, so a job that WAS
        # authorized reads as denied afterwards; say which of the two it is.
        return digest, "DENIED (authorization cleared by a hold/release rotation — re-authorize)"
    return digest, "DENIED (never operator authorized)"


def _print_list(directory, hermes_home):
    """Read-only inventory: every job, its digest, its verdict, and the one command that fixes DENIED."""
    import cron.jobs as jobs
    control = _load_control(directory)
    authorized = control.get("authorized_cron_jobs") or {}
    print(f"hold dir:   {directory}")
    print(f"held:       {control.get('held', 'unknown')}")
    print(f"generation: {control.get('generation') or 'MISSING'}")
    print(f"authorized: {len(authorized)} job(s)")
    pending, denied = [], []
    for job in jobs.load_jobs():
        job_id = str(job.get("id") or "")
        digest, verdict = _authorization_state(job, control)
        if verdict == "authorized":
            print(f"  {job_id}  authorized   {digest}")
            continue
        print(f"  {job_id}  {verdict}  {job.get('name') or ''}")
        denied.append(job_id)
        if digest is not None:
            pending.append(job_id)
    rejected = sorted(set(denied) - set(pending))
    if rejected:
        print("\nNever authorizable (script/monitor/skill/context_from payloads) — replace with a"
              " self-contained prompt instead:")
        for job_id in rejected:
            print(f"  {job_id}")
    print("\nNot-yet-authorized jobs: " + (", ".join(pending) or "none"))
    if pending:
        print("\nAuthorize them all in ONE command (add/remove --job-id to change the set):")
        print(f"  python3 {Path(__file__).resolve()} {directory} authorize-cron \\")
        for job_id in pending:
            print(f"      --job-id {job_id} \\")
        print(f"      --hermes-home {Path(hermes_home).resolve()}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=("hold", "release", "authorize-cron", "authorize-extra", "list"))
    parser.add_argument("--job-id", dest="job_ids", action="append", default=[],
                        help="One exact existing job ID; repeat the flag for several jobs "
                             "(all-or-nothing). Never a name or wildcard")
    parser.add_argument("--hermes-home", type=Path, help="Host-side Hermes home containing cron/jobs.json")
    parser.add_argument("--extra-prompt", metavar="TEXT",
                        help="authorize-extra: the exact per-fire instruction text to approve")
    parser.add_argument("--extra-prompt-file", type=Path, metavar="PATH",
                        help="authorize-extra: file whose exact contents to approve (keeps the text "
                             "out of shell history)")
    parser.add_argument("--include-job-extra-prompt", action="store_true",
                        help="authorize-extra: approve the per-fire prompt currently queued on the job")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "list":
        if not args.hermes_home:
            parser.error("list requires --hermes-home")
        sys.path.insert(0, _SRC_ROOT)
        os.environ["HERMES_HOME"] = str(args.hermes_home.resolve())
        return _print_list(directory, args.hermes_home)
    directory.mkdir(parents=True, exist_ok=True)
    import fcntl
    # Serialize all host writers, including approval read/modify/write updates.
    lock_fd = os.open(directory / ".operator.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    held = args.action == "hold"
    control = {"generation": uuid.uuid4().hex, "held": held, "authorized_cron_jobs": {},
               "authorized_cron_extras": {}}
    if args.action == "authorize-cron":
        if not args.job_ids or not args.hermes_home:
            parser.error("authorize-cron requires --job-id and --hermes-home")
        sys.path.insert(0, _SRC_ROOT)
        os.environ["HERMES_HOME"] = str(args.hermes_home.resolve())
        from cron import jobs
        from agent.operator_hold import cron_payload_digest
        control = _load_control(directory)
        if control.get("held") is not False or not control.get("generation"):
            parser.error("release with a fresh generation before explicitly authorizing a job")
        with jobs._jobs_lock():
            records = jobs.load_jobs()
            # Validate the WHOLE batch (exact id, authorizable payload) before writing anything.
            selected = []
            for job_id in args.job_ids:
                matches = [job for job in records if job.get("id") == job_id]
                if len(matches) != 1:
                    parser.error(f"job-id must match exactly one existing job: {job_id}")
                job = matches[0]
                try:
                    selected.append((job, cron_payload_digest(job)))
                except ValueError as exc:
                    parser.error(f"{job_id} cannot be operator authorized: {exc}")
            authorized = control.setdefault("authorized_cron_jobs", {})
            for job, digest in selected:
                job["operator_generation"] = control["generation"]
                authorized[job["id"]] = digest
            jobs.save_jobs(records)
            # This is an approval update within the current generation, not release.
            # Run this single-writer host utility serially with hold/release operations.
    if args.action == "authorize-extra":
        if not args.job_ids:
            parser.error("authorize-extra requires --job-id")
        sources = [args.extra_prompt is not None, args.extra_prompt_file is not None,
                   args.include_job_extra_prompt]
        if sum(sources) != 1:
            parser.error("authorize-extra takes exactly one of --extra-prompt, "
                         "--extra-prompt-file, --include-job-extra-prompt")
        control = _load_control(directory)
        if control.get("held") is not False or not control.get("generation"):
            parser.error("release with a fresh generation before authorizing instructions")

        texts = {}
        if args.extra_prompt_file is not None:
            text = args.extra_prompt_file.read_text(encoding="utf-8")
            texts = {job_id: text for job_id in args.job_ids}
        elif args.extra_prompt is not None:
            texts = {job_id: args.extra_prompt for job_id in args.job_ids}
        else:
            if not args.hermes_home:
                parser.error("--include-job-extra-prompt requires --hermes-home")
            sys.path.insert(0, _SRC_ROOT)
            os.environ["HERMES_HOME"] = str(args.hermes_home.resolve())
            import cron.jobs as jobs
            with jobs._jobs_lock():
                records = jobs.load_jobs()
            for job_id in args.job_ids:
                matches = [job for job in records if job.get("id") == job_id]
                if len(matches) != 1:
                    parser.error(f"job-id must match exactly one existing job: {job_id}")
                queued = matches[0].get("manual_run_prompt")
                if not queued:
                    parser.error(f"{job_id} has no queued per-fire prompt to authorize")
                texts[job_id] = str(queued)

        extras = control.setdefault("authorized_cron_extras", {})
        for job_id, text in texts.items():
            digests = extras.setdefault(job_id, [])
            digest = _extra_prompt_digest(text)
            if digest not in digests:
                digests.append(digest)
            print(f"authorized extra prompt for {job_id}: {digest}")
    fd, temporary = tempfile.mkstemp(prefix=".control-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(control, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "control.json")
        # Releasing the marker happens AFTER durable generation publication.
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
            if args.action == "release":
                try:
                    (directory / "active").unlink()
                except FileNotFoundError:
                    pass
                os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    print(json.dumps(control))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
