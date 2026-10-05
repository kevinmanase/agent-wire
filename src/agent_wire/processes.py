# SPDX-License-Identifier: AGPL-3.0-only
"""Portable process checks for Linux and macOS: os.kill and ps, not /proc."""

import os
import subprocess
from pathlib import Path


def ps(*args: str) -> str:
    # UTC keeps a recorded start time stable when the machine's time zone changes.
    return subprocess.run(
        ["ps", *args],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    ).stdout


def process_starts(pids) -> dict[int, str | None]:
    """Map each running process to its start time, or None when ps can't read it.

    A process that no longer exists is left out.
    """
    starts = {}
    for pid in set(pids):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass  # It exists, but as another user's process; its start time tells.
        starts[pid] = None
    if starts:
        try:
            output = ps("-o", "pid=", "-o", "lstart=", "-p", ",".join(map(str, starts)))
        except (OSError, subprocess.SubprocessError):
            return starts
        for line in output.splitlines():
            pid, _, start = line.strip().partition(" ")
            if pid.isdigit() and int(pid) in starts:
                starts[int(pid)] = " ".join(start.split())
    return starts


def ancestor(name: str) -> int | None:
    """The nearest ancestor of this process whose executable is called `name`."""
    try:
        output = ps("-A", "-o", "pid=", "-o", "ppid=", "-o", "comm=")
    except (OSError, subprocess.SubprocessError):
        return None
    parents, names = {}, {}
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            pid, ppid = int(fields[0]), int(fields[1])
            parents[pid], names[pid] = ppid, Path(fields[2].strip()).name
    pid = os.getppid()
    while pid > 1:
        if names.get(pid) == name:
            return pid
        pid = parents.get(pid, 0)
    return None
