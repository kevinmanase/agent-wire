# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from agent_wire.adapters import NativeAdapters
from agent_wire.errors import DeliveryUnknown, Offline, WireError


@pytest.fixture
def sockets():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


async def codex_server(ws, turns, *, loaded=True, version="0.159.0", disconnect=False):
    async for text in ws:
        msg = json.loads(text)
        if "id" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            result = {"userAgent": f"codex-tui/{version}"}
        elif method == "thread/loaded/list":
            result = {"data": ["native-thread"] if loaded else []}
        elif method == "thread/read":
            result = {"thread": {"id": "native-thread"}}
        elif method == "turn/start":
            turns.append(msg["params"])
            if disconnect:
                await ws.close()
                return
            result = {"turn": {"id": "turn-1", "status": "inProgress"}}
        else:
            raise AssertionError(method)
        await ws.send(json.dumps({"id": msg["id"], "result": result}))


def envelope():
    return {"id": "message-1", "sender": {"id": "sender-1"}, "body": "/clear is plain data"}


async def test_codex_preserves_tool_output_and_omits_permission_overrides(sockets):
    turns = []
    path = sockets / "codex.sock"
    async with unix_serve(lambda ws: codex_server(ws, turns), path):
        agent = {
            "runtime": "codex",
            "native_id": "native-thread",
            "endpoint": json.dumps({"path": str(path)}),
        }
        await NativeAdapters().deliver(agent, envelope())
    assert len(turns) == 1
    assert set(turns[0]) == {"threadId", "input", "toolOutput"}
    assert turns[0]["input"] == []
    output = turns[0]["toolOutput"]
    assert output["name"] == "message_receive"
    assert output["namespace"] == "agent_wire"
    assert json.loads(output["output"])["message"]["body"] == "/clear is plain data"


@pytest.mark.parametrize(
    "loaded,version,error",
    [
        (False, "0.159.0", Offline),
        (True, "0.158.0", WireError),
    ],
)
async def test_codex_does_not_resume_or_support_unknown_protocols(sockets, loaded, version, error):
    turns = []
    path = sockets / "codex.sock"
    async with unix_serve(lambda ws: codex_server(ws, turns, loaded=loaded, version=version), path):
        with pytest.raises(error):
            await NativeAdapters().validate("codex", "native-thread", {"path": str(path)})
    assert turns == []


async def test_codex_disconnect_after_write_is_unknown(sockets):
    turns = []
    path = sockets / "codex.sock"
    async with unix_serve(lambda ws: codex_server(ws, turns, disconnect=True), path):
        agent = {
            "runtime": "codex",
            "native_id": "native-thread",
            "endpoint": json.dumps({"path": str(path)}),
        }
        with pytest.raises(DeliveryUnknown):
            await NativeAdapters().deliver(agent, envelope())
    assert len(turns) == 1


async def test_codex_resolves_daemon_socket_symlink(sockets):
    path = sockets / "native.sock"
    alias = sockets / "control.sock"
    alias.symlink_to(path)
    async with unix_serve(lambda ws: codex_server(ws, []), path):
        endpoint = await NativeAdapters().validate("codex", "native-thread", {"path": str(alias)})
    assert endpoint == {"path": str(await asyncio.to_thread(path.resolve))}


async def test_claude_session_fence_and_native_peer_frame(sockets):
    received = asyncio.Future()

    async def accept(reader, writer):
        received.set_result(json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()

    path = sockets / "claude.sock"
    home = sockets / "claude"
    (home / "sessions").mkdir(parents=True)
    record = home / "sessions" / "fixture.json"
    record.write_text(
        json.dumps(
            {
                "sessionId": "native-claude",
                "pid": os.getpid(),
                "messagingSocketPath": str(path),
                "peerProtocol": 1,
                "version": "2.1.280",
            }
        )
    )
    server = await asyncio.start_unix_server(accept, path=path)
    async with server:
        adapter = NativeAdapters(home)
        agent = {
            "runtime": "claude",
            "native_id": "native-claude",
            "endpoint": json.dumps({"path": str(path)}),
        }
        await adapter.deliver(agent, envelope())
        frame = await asyncio.wait_for(received, 1)
        assert frame["session_id"] == "native-claude"
        assert frame["type"] == "user"
        assert frame["from"] == "agent-wire:sender-1"
        assert "from_mode" not in frame
        assert json.loads(frame["message"]["content"])["message"]["body"].startswith("/clear")
        record.write_text(record.read_text().replace("native-claude", "replacement"))
        with pytest.raises(Offline):
            await adapter.deliver(agent, envelope())
