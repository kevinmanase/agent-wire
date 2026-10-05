# SPDX-License-Identifier: AGPL-3.0-only
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_wire.broker import Broker
from agent_wire.errors import WireError
from agent_wire.hooks import run_hook
from agent_wire.processes import ancestor, process_starts
from agent_wire.store import Store

from .test_broker import Adapter
from .test_store import enroll, send

START = "Mon Oct 5 09:00:00 2026"


class Processes:
    """A fake process table: pid -> start time, or None when ps can't read it."""

    def __init__(self, **table):
        self.table = {int(pid.lstrip("p")): start for pid, start in table.items()}
        self.calls = []

    def __call__(self, pids):
        self.calls.append(sorted(pids))
        return {pid: self.table[pid] for pid in pids if pid in self.table}


@pytest.fixture
def processes():
    return Processes(p100=START, p200=START, p300=START, p400=START)


@pytest.fixture
def store(tmp_path, processes):
    db = Store(tmp_path / "db", processes=processes)
    yield db
    db.close()


def run(store, name, pid, runtime="claude"):
    return store.register(name, runtime, str(uuid.uuid4()), {}, "", pid=pid)


def active(store):
    return {agent["name"] for agent in store.agents()}


def test_exited_sessions_retire_and_their_messages_fail(store, processes):
    live, dead, reused = run(store, "live", 100), run(store, "dead", 200), run(store, "reused", 300)
    unreadable, mailbox = run(store, "unreadable", 400), enroll(store, "mailbox")
    assert store.agent(live["agent"]["id"])["started"] == START
    queued = send(store, live, dead)
    del processes.table[200]
    processes.table[300] = "Tue Oct 6 10:00:00 2026"  # The pid now names a new process.
    processes.table[400] = None  # Running, but its start time can't be read.
    listed = {s["name"] for s in store.sessions(include_finished=True)["sessions"]}
    assert listed == {"live", "unreadable", "mailbox"}
    assert active(store) == listed
    store.expire()
    assert store.status(live["session_handle"], queued["id"])["status"] == "failed"
    with pytest.raises(WireError, match="retired"):
        store.session_update(dead["session_handle"], task="Still here", status="working")
    with pytest.raises(WireError, match="retired"):
        send(store, mailbox, reused)
    assert store.agent(unreadable["agent"]["id"])["active"] == 1


def test_routing_checks_only_the_recipient(store, processes):
    a, b, c = run(store, "a", 100), run(store, "b", 200), run(store, "c", 300)
    processes.calls.clear()
    send(store, a, b)
    assert processes.calls == [[200]]
    del processes.table[300]
    with pytest.raises(WireError, match="retired"):
        send(store, a, c)
    assert active(store) == {"a", "b"}


def test_a_dead_or_missing_process_is_not_recorded(store):
    assert store.agent(run(store, "gone", 999)["agent"]["id"])["pid"] is None
    assert store.agent(run(store, "unknown", None)["agent"]["id"])["pid"] is None
    with pytest.raises(WireError, match="pid"):
        run(store, "bad", -1)
    assert active(store) == {"gone", "unknown"}


def test_a_resumed_conversation_records_its_new_process(store, processes):
    a = run(store, "a", 100)
    store.refresh_endpoint(a["session_handle"], {}, "/repo", 200)
    del processes.table[100]
    assert active(store) == {"a"}
    del processes.table[200]
    assert active(store) == set()


class Natives(Adapter):
    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    async def validate(self, runtime, native_id, endpoint):
        return {**endpoint, "pid": self.pid} if runtime == "claude" else endpoint


async def test_broker_takes_claude_pids_from_native_records(store):
    broker = Broker(store, Natives(100))
    params = dict(native_id="native", endpoint={"path": "/s"}, cwd="", pid=200)
    claude = await broker.call("register", dict(name="c", runtime="claude", **params))
    codex = await broker.call("register", dict(name="x", runtime="codex", **params))
    assert store.agent(claude["agent"]["id"])["pid"] == 100
    assert store.agent(codex["agent"]["id"])["pid"] == 200
    assert "pid" not in store.agent(claude["agent"]["id"])["endpoint"]
    await broker.call(
        "session_refresh",
        dict(session_handle=codex["session_handle"], endpoint={"path": "/s"}, cwd="", pid=300),
    )
    assert store.agent(codex["agent"]["id"])["pid"] == 300


async def test_delivery_to_an_exited_session_fails_without_writing(store, processes):
    a, b = enroll(store), run(store, "b", 200, "codex")
    message = send(store, a, b)
    del processes.table[200]
    adapter = Adapter()
    await Broker(store, adapter).deliver_pending()
    assert adapter.delivered == []
    assert store.status(a["session_handle"], message["id"])["status"] == "failed"


async def test_codex_hook_records_its_runtime_process(environment, monkeypatch):
    state, store = environment
    target = {"runtime": "codex", "native_id": "t", "endpoint": {"path": "/s"}, "cwd": ""}

    async def discover(self, codex_socket):
        return {"sessions": [target]}

    async def validate(self, runtime, native_id, endpoint):
        return endpoint

    monkeypatch.setattr("agent_wire.hooks.NativeAdapters.discover", discover)
    monkeypatch.setattr("agent_wire.adapters.NativeAdapters.validate", validate)
    monkeypatch.setattr("agent_wire.hooks.ancestor", lambda name: os.getpid())
    await run_hook(state, "codex", {"session_id": "t", "hook_event_name": "SessionStart"})
    agent = store.agents()[0]
    assert store.agent(agent["id"])["pid"] == os.getpid()


def test_real_process_table():
    own = process_starts([os.getpid(), os.getpid()])
    assert list(own) == [os.getpid()] and own[os.getpid()]
    assert process_starts([os.getpid()]) == own
    child = subprocess.Popen(["true"])
    child.wait()
    assert process_starts([child.pid]) == {}
    parent = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(os.getppid())], capture_output=True, text=True
    ).stdout
    assert ancestor(Path(parent.strip()).name) == os.getppid()
