# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

from agent_wire.broker import Broker
from agent_wire.mcp_server import make_server
from agent_wire.paths import write_identity
from agent_wire.store import FOLD_AFTER, Store

from .test_store import enroll


async def test_same_mcp_tools_can_send_receive_ack_and_reply():
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        store = Store(state / "db")
        a, b = enroll(store), enroll(store, "b")
        broker = Broker(store)
        server = await asyncio.start_unix_server(broker.handle, path=state / "broker.sock")
        async with server, Client(make_server(state), raise_exceptions=True) as client:
            result = await client.call_tool(
                "message_send",
                {
                    "session_handle": a["session_handle"],
                    "to": "b",
                    "body": "hello",
                    "idempotency_key": "mcp-send",
                },
            )
            message_id = result.structured_content["id"]
            incoming = await client.call_tool(
                "messages_read", {"session_handle": b["session_handle"]}
            )
            assert incoming.structured_content["messages"][0]["id"] == message_id
            ack = await client.call_tool(
                "message_ack",
                {
                    "session_handle": b["session_handle"],
                    "message_id": message_id,
                },
            )
            assert ack.structured_content["status"] == "acknowledged"
            reply = await client.call_tool(
                "message_send",
                {
                    "session_handle": b["session_handle"],
                    "to": "a",
                    "body": "reply",
                    "in_reply_to": message_id,
                },
            )
            assert reply.structured_content["in_reply_to"] == message_id
        store.close()


async def test_bound_stdio_server_uses_its_own_identity():
    import sys

    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        store = Store(state / "db")
        a = enroll(store)
        identity = write_identity(state, a)
        broker = Broker(store)
        server = await asyncio.start_unix_server(broker.handle, path=state / "broker.sock")
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_wire",
                "--state",
                str(state),
                "mcp",
                "--identity",
                str(identity),
            ],
        )
        async with server, Client(params) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "agents_list",
                "message_send",
                "message_ack",
                "messages_read",
                "message_status",
                "sessions_list",
                "session_update",
            }
            result = await client.call_tool("agents_list", {})
            assert result.structured_content["agents"][0]["name"] == "a"
            result = await client.call_tool("agents_list", {"session_handle": "someone-else"})
            assert result.is_error
            result = await client.call_tool(
                "session_update",
                {
                    "task": "Bound report",
                    "status": "working",
                    "session_handle": "someone-else",
                },
            )
            assert result.is_error
            result = await client.call_tool(
                "session_update", {"task": "Bound report", "status": "working"}
            )
            assert result.structured_content["name"] == "a"
            listing = await client.call_tool("sessions_list", {})
            assert listing.structured_content["sessions"][0]["report"]["task"] == "Bound report"
            ask = {"to": "kevin", "text": "Approve the merge?", "kind": "approve"}
            result = await client.call_tool(
                "session_update",
                {
                    "task": "Bound report",
                    "status": "waiting",
                    "lane": "api",
                    "stage": "REVIEW",
                    "role": "worker",
                    "ask": ask,
                },
            )
            report = result.structured_content["report"]
            assert (report["lane"], report["stage"], report["role"]) == ("api", "REVIEW", "worker")
            assert report["ask"] == {**ask, "raised_at": report["reported_at"]}
            for bad in ({"role": "boss"}, {"ask": {**ask, "kind": "merge"}}):
                result = await client.call_tool(
                    "session_update", {"task": "Bound report", "status": "waiting", **bad}
                )
                assert result.is_error
            listing = await client.call_tool("sessions_list", {})
            assert listing.structured_content["sessions"][0]["report"] == report
        store.close()


async def test_sessions_list_folds_finished_sessions_unless_asked():
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        store = Store(state / "db")
        now = [1000.0]
        store.clock = lambda: now[0]
        a, b = enroll(store), enroll(store, "b")
        store.session_update(a["session_handle"], task="Shipped", status="done")
        store.session_update(b["session_handle"], task="Working", status="working")
        now[0] += FOLD_AFTER + 1
        broker = Broker(store)
        server = await asyncio.start_unix_server(broker.handle, path=state / "broker.sock")
        async with server, Client(make_server(state), raise_exceptions=True) as client:
            folded = (await client.call_tool("sessions_list", {})).structured_content
            assert [s["name"] for s in folded["sessions"]] == ["b"]
            assert folded["hidden_finished"] == 1
            result = await client.call_tool("sessions_list", {"include_finished": True})
            everything = result.structured_content
            assert {s["name"] for s in everything["sessions"]} == {"a", "b"}
            assert everything["hidden_finished"] == 0
        store.close()
