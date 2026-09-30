# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json

import pytest

from agent_wire.broker import Broker
from agent_wire.errors import DeliveryUnknown, Offline, WireError
from agent_wire.store import Store

from .test_store import enroll, send


class Adapter:
    def __init__(self, error=None, callback=None):
        self.error = error
        self.callback = callback
        self.delivered = []
        self.modes = []

    async def deliver(self, agent, envelope, sender_mode=None):
        self.delivered.append(envelope)
        self.modes.append(sender_mode)
        if self.callback:
            self.callback(envelope)
        if self.error:
            raise self.error


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "submitted"),
        (Offline("no socket"), "queued"),
        (DeliveryUnknown("write interrupted"), "unknown"),
        (WireError("refused", "policy refused"), "failed"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_delivery_outcomes(tmp_path, error, expected):
    store = Store(tmp_path / "db")
    try:
        a, b = enroll(store), enroll(store, "b", "codex")
        message = send(store, a, b)
        adapter = Adapter(error)
        broker = Broker(store, adapter)
        await broker.deliver_pending()
        assert store.status(a["session_handle"], message["id"])["status"] == expected
        if expected != "queued":
            await broker.deliver_pending()
            assert len(adapter.delivered) == 1
    finally:
        store.close()


async def test_early_ack_is_not_overwritten_by_submission(tmp_path):
    store = Store(tmp_path / "db")
    try:
        a, b = enroll(store), enroll(store, "b", "claude")
        message = send(store, a, b)
        adapter = Adapter(callback=lambda msg: store.ack(b["session_handle"], msg["id"]))
        await Broker(store, adapter).deliver_pending()
        assert store.status(a["session_handle"], message["id"])["status"] == "acknowledged"
    finally:
        store.close()


async def test_slow_recipient_does_not_block_another_recipient(tmp_path):
    store = Store(tmp_path / "db")
    a, slow, fast = enroll(store), enroll(store, "slow", "codex"), enroll(store, "fast", "codex")
    first, second = send(store, a, slow), send(store, a, slow)
    other = send(store, a, fast)
    release = asyncio.Event()
    fast_done = asyncio.Event()
    delivered = []

    class SlowAdapter:
        async def deliver(self, agent, envelope, sender_mode=None):
            delivered.append(envelope["id"])
            if agent["name"] == "slow":
                await release.wait()
                raise Offline("temporarily unavailable")
            fast_done.set()

    job = asyncio.create_task(Broker(store, SlowAdapter()).deliver_pending())
    try:
        await asyncio.wait_for(fast_done.wait(), 1)
        assert not job.done()
        release.set()
        await job
        assert set(delivered) == {first["id"], other["id"]}
        assert store.message(second["id"])["status"] == "queued"
    finally:
        release.set()
        await job
        store.close()


async def test_retired_recipient_is_not_replaced_by_same_name(tmp_path):
    store = Store(tmp_path / "db")
    try:
        a, b = enroll(store), enroll(store, "b", "codex")
        message = send(store, a, b)
        store.retire(b["session_handle"])
        enroll(store, "b", "codex")
        adapter = Adapter()
        await Broker(store, adapter).deliver_pending()
        assert adapter.delivered == []
        assert store.status(a["session_handle"], message["id"])["status"] == "failed"
    finally:
        store.close()


async def test_malformed_request_does_not_stop_broker(tmp_path):
    store = Store(tmp_path / "db")
    broker = Broker(store)
    # A short path keeps Unix sockets under the OS limit on every CI runner.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        socket = str(Path(d) / "s")
        server = await asyncio.start_unix_server(broker.handle, path=socket)
        async with server:
            for frame in (b"not-json\n", b'{"method":"x","params":[]}\n'):
                reader, writer = await asyncio.open_unix_connection(socket)
                writer.write(frame)
                await writer.drain()
                response = json.loads(await reader.readline())
                assert response["error"]["code"] == "invalid_input"
                writer.close()
                await writer.wait_closed()
        store.close()


async def test_delivery_attests_the_senders_latest_mode(tmp_path):
    store = Store(tmp_path / "db")
    try:
        a, b = enroll(store), enroll(store, "b", "claude")
        adapter = Adapter()
        broker = Broker(store, adapter)
        for mode in ("bypass", None):
            store.session_heartbeat(a["session_handle"], activity="working", mode=mode)
            send(store, a, b)
            await broker.deliver_pending()
            store.ack(b["session_handle"], adapter.delivered[-1]["id"])
        assert adapter.modes == ["bypass", None]
        with pytest.raises(WireError):
            store.session_heartbeat(
                b["session_handle"], activity="working", mode="bypassPermissions"
            )
        assert "mode" not in adapter.delivered[0]["sender"]
    finally:
        store.close()
