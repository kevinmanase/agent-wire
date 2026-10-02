# SPDX-License-Identifier: AGPL-3.0-only
"""Read sender permission metadata without changing either runtime's policy."""

import json
import os
import stat
import uuid
from pathlib import Path

# Rollouts can contain large tool results. Bound both record memory and total scanning.
MAX_RECORD = 2 * 1024 * 1024
MAX_SCAN = 64 * 1024 * 1024
CHUNK = 64 * 1024


def codex_mode(context: dict) -> str | None:
    """Only unrestricted execution with no approvals is equivalent to Claude bypass."""
    approval = context.get("approval_policy")
    sandbox = context.get("sandbox_policy")
    if not isinstance(sandbox, dict):
        return None
    sandbox_type = sandbox.get("type")
    if sandbox_type not in ("danger-full-access", "workspace-write", "read-only"):
        # In particular, an external sandbox does not describe its enforcement here.
        return None
    if approval == "never":
        return "bypass" if sandbox_type == "danger-full-access" else "prompting"
    if approval in ("untrusted", "on-request", "on-failure"):
        return "prompting"
    return None


def latest_context(stream, size: int) -> dict | None:
    """Inspect complete JSONL records backwards; never interpret conversation text."""
    position, pending = size, b""
    floor = max(0, size - MAX_SCAN)
    stream.seek(size - 1)
    if stream.read(1) != b"\n":
        return None  # A concurrent/incomplete append is not current permission evidence.
    while position > floor:
        length = min(CHUNK, position - floor)
        position -= length
        stream.seek(position)
        parts = (stream.read(length) + pending).split(b"\n")
        pending = parts[0]
        for line in reversed(parts[1:]):
            if not line:
                continue
            if len(line) > MAX_RECORD:
                return None
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            if record.get("type") == "turn_context":
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else None
        if len(pending) > MAX_RECORD:
            return None
    return None


def codex_session_mode(native_id: str, home: Path | None = None) -> str | None:
    """Resolve an enrolled Codex UUID, including a Desktop CLI-mailbox enrollment.

    The runtime's session_meta must match the explicit enrollment. Read only its
    latest turn_context; prompts, tool results, global config, and peer claims are
    never permission evidence. No identity file or credential is read here.
    """
    try:
        if str(uuid.UUID(native_id)) != native_id:
            return None
        home = home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        sessions = (home / "sessions").resolve()
        paths = list(sessions.glob(f"????/??/??/rollout-*-{native_id}.jsonl"))
        if len(paths) != 1 or not paths[0].resolve().is_relative_to(sessions):
            return None
        fd = os.open(paths[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_size:
                return None
            first = stream.readline(MAX_RECORD + 1)
            if len(first) > MAX_RECORD:
                return None
            meta = json.loads(first)
            if (
                not isinstance(meta, dict)
                or meta.get("type") != "session_meta"
                or not isinstance(meta.get("payload"), dict)
                or meta["payload"].get("id") != native_id
            ):
                return None
            context = latest_context(stream, info.st_size)
            return codex_mode(context) if context is not None else None
    except (OSError, ValueError, TypeError):
        return None


def sender_mode(agent) -> str | None:
    if agent["runtime"] == "claude":
        return agent["mode"]
    if agent["runtime"] in ("codex", "mailbox"):
        return codex_session_mode(agent["native_id"])
    return None
