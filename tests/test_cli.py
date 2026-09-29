# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agent_wire.client import call
from agent_wire.errors import WireError
from agent_wire.paths import private_directory, read_identity


async def cli(state, *args, stdin=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_wire",
        "--state",
        str(state),
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


async def start_broker(state):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_wire",
        "--state",
        str(state),
        "serve",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(100):
            try:
                assert (await call(state, "ping"))["protocol"] == 1
                return process
            except WireError:
                await asyncio.sleep(0.05)
        raise AssertionError("Broker did not start")
    except BaseException:
        if process.returncode is None:
            process.terminate()
        await process.communicate()
        raise


async def stop_broker(process):
    if process.returncode is None:
        process.terminate()
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, (stdout, stderr)


async def test_cli_roundtrip_restart_permissions_and_single_owner():
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        process = await start_broker(state)
        try:
            assert (state / "broker.sock").stat().st_mode & 0o077 == 0
            assert (state / "messages.sqlite3").stat().st_mode & 0o077 == 0
            duplicate = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_wire",
                "--state",
                str(state),
                "serve",
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await asyncio.wait_for(duplicate.communicate(), 5)
            assert duplicate.returncode == 1
            assert json.loads(error)["error"]["code"] == "already_running"
            identities = []
            for name in ("alice", "bob"):
                result = await cli(
                    state,
                    "register",
                    "--name",
                    name,
                    "--runtime",
                    "mailbox",
                    "--session",
                    str(uuid.uuid4()),
                )
                path = Path(result["identity_file"])
                assert (await asyncio.to_thread(path.stat)).st_mode & 0o077 == 0
                assert "session_handle" not in result
                identities.append(str(path))
            alice, bob = identities
            await cli(
                state,
                "report",
                "--identity",
                alice,
                "--task",
                "CLI registry test",
                "--status",
                "blocked",
                "--detail",
                "Need review",
            )
            sessions = await cli(state, "sessions", "--status", "blocked")
            assert len(sessions["sessions"]) == 1
            assert sessions["sessions"][0]["name"] == "alice"
            message = await cli(
                state,
                "send",
                "--identity",
                alice,
                "--to",
                "bob",
                "--body",
                "test",
                "--key",
                "cli-restart",
            )
            await stop_broker(process)
            process = await start_broker(state)
            sessions = await cli(state, "sessions", "--status", "blocked")
            assert sessions["sessions"][0]["report"]["task"] == "CLI registry test"
            inbox = await cli(state, "inbox", "--identity", bob)
            assert inbox["messages"][0]["id"] == message["id"]
            duplicate = await cli(
                state,
                "send",
                "--identity",
                alice,
                "--to",
                "bob",
                "--body",
                "test",
                "--key",
                "cli-restart",
            )
            assert duplicate["id"] == message["id"]
            reply = await cli(
                state,
                "send",
                "--identity",
                bob,
                "--to",
                "alice",
                "--body",
                "reply",
                "--reply-to",
                message["id"],
            )
            ack = await cli(state, "ack", "--identity", alice, reply["id"])
            assert ack["status"] == "acknowledged"
            status = await cli(state, "status", "--identity", alice, message["id"])
            assert status["status"] == "replied"
            assert status["reply_id"] == reply["id"]
        finally:
            await stop_broker(process)


async def test_hook_failure_leaves_session_running(tmp_path):
    result = await cli(tmp_path, "hook", "codex", stdin=b"{}")
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result


def test_identity_rejects_public_file_and_symlink(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text('{"session_handle":"private"}')
    os.chmod(path, 0o644)
    with pytest.raises(WireError, match="private"):
        read_identity(path)
    os.chmod(path, 0o600)
    assert read_identity(path) == "private"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_identity(link)


def test_state_directory_must_be_private(tmp_path):
    os.chmod(tmp_path, 0o755)
    with pytest.raises(WireError, match="private directory"):
        private_directory(tmp_path)
