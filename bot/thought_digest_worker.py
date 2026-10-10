#!/usr/bin/env python3
"""Mac-only scheduled processor for immutable thought digest jobs."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

import thought_digest_jobs as jobs


def result_path(vault: Path | str, job_id: str) -> Path:
    return jobs.result_path(vault, job_id)


def _git(command: list[str], vault: Path | str, *, capture: bool = False):
    try:
        return subprocess.run(command, cwd=Path(vault), timeout=45, check=False,
                              stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, text=capture)
    except (OSError, subprocess.SubprocessError):
        return None


def _latest_result(vault: Path | str) -> Path | None:
    directory = Path(vault).absolute() / jobs.QUEUE_ROOT / "results"
    try:
        candidates = []
        for path in directory.glob("*.json"):
            record = jobs.get_result(vault, path.stem)
            if record is not None:
                candidates.append((record["completed"], path))
        return max(candidates, default=("", None))[1]
    except (OSError, ValueError):
        return None


def _result_needs_sync(vault: Path | str) -> Path | None:
    directory = Path(vault).absolute() / jobs.QUEUE_ROOT / "results"
    status = _git(["git", "status", "--porcelain", "--untracked-files=all", "--", str(directory)],
                  vault, capture=True)
    if status is not None and status.returncode == 0:
        root = Path(vault).absolute()
        for line in (status.stdout or "").splitlines():
            candidate = line[3:].split(" -> ")[-1].strip()
            path = root / candidate
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if (relative.parts[:3] == ("SYSTEM", "THOUGHT_DIGEST", "results")
                    and len(relative.parts) == 4 and jobs.get_result(vault, path.stem) is not None):
                return path
    ahead = _git(["git", "log", "-1", "--format=%H", "@{upstream}..HEAD", "--", str(directory)],
                 vault, capture=True)
    if ahead is not None and ahead.returncode == 0 and (ahead.stdout or "").strip():
        return _latest_result(vault)
    return None


def run_worker(vault: Path | str, generate=None) -> int:
    """Pull, process at most one request, then explicitly sync its result."""
    if platform.system() != "Darwin":
        return 2
    vault = Path(vault).absolute()
    with jobs.worker_lock(vault):
        pulled = _git(["git", "pull", "--rebase", "--autostash"], vault)
        if pulled is None or pulled.returncode != 0:
            return 3
        previous = _result_needs_sync(vault)
        if previous is not None:
            return 0 if jobs.sync_file(vault, previous) else 4
        # Personal note content is always sent to the existing local Vertex path.
        os.environ["LLM_PROVIDER"] = "vertex"
        try:
            job_id = jobs.process_one(vault, generate=generate)
        except jobs.QueueSafetyError:
            return 6
        if not job_id:
            return 0
        return 0 if jobs.sync_file(vault, result_path(vault, job_id)) else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process one local thought digest request")
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)
    vault = Path(args.vault).expanduser().absolute()
    try:
        from dotenv import load_dotenv
        load_dotenv(vault / ".env", override=False)
    except ImportError:
        print("thought-digest worker: dotenv unavailable", file=sys.stderr)
        return 5
    return run_worker(vault)


if __name__ == "__main__":
    raise SystemExit(main())
