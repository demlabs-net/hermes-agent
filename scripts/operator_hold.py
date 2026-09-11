#!/usr/bin/env python3
"""Operator-host-only control writer. Never mount this directory writable in agents.

Usage:
  python3 scripts/operator_hold.py /host/operator-hold hold
  python3 scripts/operator_hold.py /host/operator-hold release
  python3 scripts/operator_hold.py /host/operator-hold authorize-cron \
      --job-id <exact id> --hermes-home /host/hermes-home

Provision control.json before starting a configured agent (init is implicit: the
first hold/release writes the file). Directory mount into containers must be
read-only (mount the directory, not an individual inode). Every hold/release
rotates the generation; release also clears the cron authorizations and removes
the legacy active marker. An environment hold still wins.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=("hold", "release", "authorize-cron"))
    parser.add_argument("--job-id", help="One exact existing job ID; never a name or wildcard")
    parser.add_argument("--hermes-home", type=Path, help="Host-side Hermes home containing cron/jobs.json")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    import fcntl
    # Serialize all host writers, including approval read/modify/write updates.
    lock_fd = os.open(directory / ".operator.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    held = args.action == "hold"
    control = {"generation": uuid.uuid4().hex, "held": held, "authorized_cron_jobs": {}}
    if args.action == "authorize-cron":
        if not args.job_id or not args.hermes_home:
            parser.error("authorize-cron requires --job-id and --hermes-home")
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        os.environ["HERMES_HOME"] = str(args.hermes_home.resolve())
        from cron import jobs
        from agent.operator_hold import cron_payload_digest
        control = json.loads((directory / "control.json").read_text())
        if control.get("held") is not False or not control.get("generation"):
            parser.error("release with a fresh generation before explicitly authorizing a job")
        with jobs._jobs_lock():
            records = jobs.load_jobs()
            selected = [job for job in records if job.get("id") == args.job_id]
            if len(selected) != 1:
                parser.error("job-id must match exactly one existing job")
            job = selected[0]
            digest = cron_payload_digest(job)
            job["operator_generation"] = control["generation"]
            jobs.save_jobs(records)
            control.setdefault("authorized_cron_jobs", {})[args.job_id] = digest
        # This is an approval update within the current generation, not release.
        # Run this single-writer host utility serially with hold/release operations.
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


if __name__ == "__main__":
    main()
