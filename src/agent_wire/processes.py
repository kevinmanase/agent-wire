# SPDX-License-Identifier: AGPL-3.0-only
"""Portable process checks for Linux and macOS: os.kill and ps, not /proc."""

import calendar
import os
import subprocess
import time
from pathlib import Path


def ps(*args: str) -> str:
    # Bounded: the broker calls this on its event loop.
    return subprocess.run(
        ["ps", *args],
        capture_output=True,
        text=True,
        timeout=2,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    ).stdout


def alive(pid: int) -> bool:
    """Whether the process exists, including another user's."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # It exists, but as another user's process.
    return True


def process_starts(pids) -> dict[int, float | None]:
    """Map each running process to its start time in epoch seconds, or None when unreadable.

    A process that no longer exists is left out.
    """
    # Another user's process exists too; its start time tells.
    starts = {pid: None for pid in set(pids) if alive(pid)}
    if starts:
        try:
            output = ps("-o", "pid=", "-o", "lstart=", "-p", ",".join(map(str, starts)))
        except (OSError, subprocess.SubprocessError):
            return starts
        for line in output.splitlines():
            pid, _, start = line.strip().partition(" ")
            if pid.isdigit() and int(pid) in starts:
                try:
                    started = time.strptime(" ".join(start.split()), "%a %b %d %H:%M:%S %Y")
                except ValueError:
                    continue
                starts[int(pid)] = calendar.timegm(started)
    return starts


def is_app_server(pid: int) -> bool:
    """Whether this Codex process is an app server (the daemon or the desktop app's).

    An app server runs many threads at once; --no-daemon Codex runs one.
    If ps fails, assume an app server, so nothing is retired.
    """
    try:
        return "app-server" in ps("-ww", "-o", "args=", "-p", str(pid)).split()
    except (OSError, subprocess.SubprocessError):
        return True


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
