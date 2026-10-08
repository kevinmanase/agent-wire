# SPDX-License-Identifier: AGPL-3.0-only
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_wire.broker import Broker
from agent_wire.errors import WireError
from agent_wire.hooks import run_hook
from agent_wire.processes import ancestor, is_app_server, process_starts, ps
from agent_wire.store import Store

from .test_broker import Adapter
from .test_sessions import ASK
from .test_store import enroll, send

START = 1_791_200_000.0


class Processes:
    """A fake process table: pid -> start time, or None when ps can't read it."""

    def __init__(self, table):
        self.table = table
        self.app_servers = set()
        self.calls = []

    def __call__(self, pids):
        self.calls.append(sorted(pids))
        return {pid: self.table[pid] for pid in pids if pid in self.table}


@pytest.fixture
def processes():
    return Processes(dict.fromkeys((100, 200, 300, 400), START))


@pytest.fixture
def store(tmp_path, processes):
    db = Store(tmp_path / "db", processes=processes, app_server=processes.app_servers.__contains__)
    yield db
    db.close()


def active(store):
    return {agent["name"] for agent in store.agents()}


def test_exited_sessions_retire_and_their_messages_fail(store, processes):
    live, dead, reused = (
        enroll(store, "live", pid=100),
        enroll(store, "dead", pid=200),
        enroll(store, "reused", pid=300),
    )
    unreadable, mailbox = enroll(store, "unreadable", pid=400), enroll(store, "mailbox")
    assert store.agent(live["agent"]["id"])["started"] == START
    queued = send(store, live, dead)
    del processes.table[200]
    processes.table[100] = START + 30  # A clock step moves the same process's start.
    processes.table[300] = START + 3600  # The pid now names a new process.
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
    a, b, c = enroll(store, "a", pid=100), enroll(store, "b", pid=200), enroll(store, "c", pid=300)
    processes.calls.clear()
    send(store, a, b)
    assert processes.calls == [[200]]
    del processes.table[300]
    with pytest.raises(WireError, match="retired"):
        send(store, a, c)
    assert active(store) == {"a", "b"}


def test_a_dead_or_missing_process_is_not_recorded(store):
    assert store.agent(enroll(store, "gone", pid=999)["agent"]["id"])["pid"] is None
    assert store.agent(enroll(store, "unknown", pid=None)["agent"]["id"])["pid"] is None
    with pytest.raises(WireError, match="pid"):
        enroll(store, "bad", pid=-1)
    assert active(store) == {"gone", "unknown"}


def test_a_resumed_conversation_records_its_new_process(store, processes):
    a = enroll(store, "a", pid=100)
    store.refresh_endpoint(a["session_handle"], {}, "/repo", 200)
    del processes.table[100]
    assert active(store) == {"a"}
    del processes.table[200]
    assert active(store) == set()


def test_an_unreadable_process_keeps_the_recorded_one(store, processes):
    a = enroll(store, "a", pid=100)
    store.refresh_endpoint(a["session_handle"], {}, "/repo", None)  # The hook's ps failed.
    processes.table[100] = None  # The broker's ps timed out.
    store.refresh_endpoint(a["session_handle"], {}, "/repo", 100)
    assert store.agent(a["agent"]["id"])["pid"] == 100
    del processes.table[100]
    assert active(store) == set()


def test_an_exited_session_frees_its_name(store, processes):
    enroll(store, "worker", pid=200)
    del processes.table[200]
    replacement = enroll(store, "worker", pid=100)
    assert [a["id"] for a in store.agents()] == [replacement["agent"]["id"]]


def test_a_new_claude_conversation_replaces_the_one_in_its_process(store, processes):
    old, other = enroll(store, "worker", "claude", 100), enroll(store, "other", "claude", 200)
    enroll(store, "unrecorded", "claude")
    store.session_update(old["session_handle"], task="Ship", status="waiting", ask=ASK)
    processes.table[100] = START + 30  # A clock step moves the same process's start.
    # After /clear, the same process enrolls its new session, under the same hook --name.
    enroll(store, "worker", "claude", 100)
    assert store.agent(old["agent"]["id"])["active"] == 0
    assert active(store) == {"worker", "other", "unrecorded"}
    assert [s["report"] for s in store.sessions()["sessions"]] == [None] * 3  # No old ask.
    processes.table[200] = START + 3600  # The pid now names a new process.
    enroll(store, "successor", "claude", 200)
    assert store.agent(other["agent"]["id"])["active"] == 1


def test_a_new_terminal_codex_thread_replaces_the_one_in_its_process(store, processes):
    old = enroll(store, "worker", "codex", 100)
    store.session_update(old["session_handle"], task="Ship", status="waiting", ask=ASK)
    # After /clear, Codex started with --no-daemon enrolls its new thread from the same process.
    enroll(store, "worker", "codex", 100)
    assert store.agent(old["agent"]["id"])["active"] == 0
    assert [s["report"] for s in store.sessions()["sessions"]] == [None]  # No old ask.
    # One Codex app server, the daemon or the desktop app's, runs many threads.
    processes.app_servers.add(300)
    codex = enroll(store, "codex", "codex", 300)
    enroll(store, "codex-2", "codex", 300)
    assert store.agent(codex["agent"]["id"])["active"] == 1
    mailbox = enroll(store, "mailbox", pid=200)
    enroll(store, "mailbox-2", pid=200)
    assert store.agent(mailbox["agent"]["id"])["active"] == 1


def test_resuming_a_claude_conversation_replaces_the_one_in_its_process(store):
    resumed = enroll(store, "resumed", "claude", 100)
    enroll(store, "current", "claude", 200)
    store.refresh_endpoint(resumed["session_handle"], {}, "", 200)  # /resume inside 200.
    assert active(store) == {"resumed"}


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
    a, b = enroll(store), enroll(store, "b", "codex", 200)
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
    parent = ps("-o", "comm=", "-p", str(os.getppid()))
    assert ancestor(Path(parent.strip()).name) == os.getppid()
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "app-server"])
    try:
        assert is_app_server(server.pid) and not is_app_server(os.getpid())
    finally:
        server.kill()
        server.wait()
