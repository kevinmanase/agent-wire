# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from agent_wire.broker import Broker
from agent_wire.errors import WireError
from agent_wire.store import STALE_AFTER, Store

from .test_store import enroll


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "db")
    yield db
    db.close()


async def test_reports_are_scoped_to_credential_and_do_not_disclose_it(store):
    a, b = enroll(store, "a", "codex"), enroll(store, "b", "claude")
    broker = Broker(store)
    await broker.call(
        "session_update",
        {
            "session_handle": a["session_handle"],
            "task": "Build registry",
            "status": "working",
        },
    )
    await broker.call(
        "session_update",
        {
            "session_handle": b["session_handle"],
            "task": "Review transport",
            "status": "blocked",
            "detail": "Need socket access",
            "ticket": "TEST-1",
        },
    )
    listing = await broker.call("sessions_list", {})
    reports = {s["name"]: s["report"] for s in listing["sessions"]}
    assert reports["a"]["task"] == "Build registry"
    assert reports["b"]["status"] == "blocked"
    for secret in (a["session_handle"], b["session_handle"], "credential", "endpoint"):
        assert secret not in json.dumps(listing)
    with pytest.raises(WireError, match="Invalid or retired"):
        await broker.call(
            "session_update",
            {
                "session_handle": "fake",
                "task": "Spoof",
                "status": "done",
            },
        )
    with pytest.raises(TypeError):
        await broker.call(
            "session_update",
            {
                "session_handle": a["session_handle"],
                "agent_id": b["agent"]["id"],
                "task": "Spoof",
                "status": "done",
            },
        )
    assert store.session(b["agent"]["id"])["report"] == reports["b"]


def test_freshness_heartbeats_and_new_turn_do_not_invent_task_state(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    a = enroll(store)
    token = a["session_handle"]
    assert store.sessions()["sessions"][0]["freshness"] == "unseen"
    initial = store.session_update(
        token, task="Build registry", status="blocked", detail="Need input"
    )
    assert initial["freshness"] == "fresh"
    now[0] += STALE_AFTER
    assert store.sessions()["sessions"][0]["freshness"] == "stale"
    assert store.sessions(include_stale=False)["sessions"] == []
    heartbeat = store.session_heartbeat(token, activity="idle")
    assert heartbeat["report"] == initial["report"]
    assert heartbeat["last_seen"] == now[0]
    assert heartbeat["freshness"] == "fresh"
    prompt = store.session_heartbeat(token, activity="working", new_turn=True)
    assert prompt["report"]["needs_update"]
    assert prompt["report"]["status"] == "blocked"
    assert prompt["report"]["reported_at"] == 1000
    current = store.session_update(token, task="Review registry", status="working")
    assert not current["report"]["needs_update"]
    assert current["report"]["detail"] == ""
    assert current["report"]["reported_at"] == now[0]


def test_reports_survive_restart_but_not_reenrollment(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    a = enroll(store)
    store.session_update(a["session_handle"], task="Persistent report", status="waiting")
    store.close()
    store = Store(path)
    try:
        assert store.sessions()["sessions"][0]["report"]["status"] == "waiting"
        replacement = store.register("a", "mailbox", a["agent"]["native_id"], {})
        sessions = store.sessions()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["id"] == replacement["agent"]["id"]
        assert sessions[0]["report"] is None
        with pytest.raises(WireError, match="Invalid or retired"):
            store.session_update(a["session_handle"], task="Old session", status="done")
        store.retire(replacement["session_handle"])
        assert store.sessions()["sessions"] == []
    finally:
        store.close()


def test_filters_pagination_and_encoded_response_bound(store):
    # Escaped non-ASCII/control characters exercise the actual wire-size limit.
    for i in range(60):
        a = enroll(store, f"s{i}", "codex" if i % 2 else "claude")
        store.session_update(
            a["session_handle"],
            task="x" * 512,
            status="working" if i % 2 else "done",
            detail="\x01" * 2048,
            repository="\x02" * 4096,
        )
    seen, after = set(), ""
    while True:
        page = store.sessions(limit=100, after=after)
        assert len(json.dumps(page).encode()) < 600 * 1024
        ids = {s["id"] for s in page["sessions"]}
        assert not seen & ids
        seen |= ids
        after = page["next_after"]
        if after is None:
            break
    assert len(seen) == 60
    page = store.sessions(runtime="claude", status="working")
    assert page["sessions"] == []
    page = store.sessions(runtime="codex", status="working", limit=1)
    assert len(page["sessions"]) == 1 and page["next_after"]
    enroll(store, "unreported")
    assert store.sessions(status="unreported")["sessions"][0]["name"] == "unreported"


@pytest.mark.parametrize(
    "fields",
    [
        {"task": ""},
        {"task": "é" * 257},
        {"status": "completed"},
        {"status": []},
        {"detail": None},
        {"detail": "x" * 2049},
        {"repository": "x" * 4097},
        {"branch": "x" * 257},
        {"ticket": "x" * 257},
    ],
)
def test_invalid_reports_leave_previous_report_unchanged(store, fields):
    a = enroll(store)
    original = store.session_update(a["session_handle"], task="Valid", status="working")
    with pytest.raises(WireError, match="must be"):
        store.session_update(
            a["session_handle"], **{"task": "Valid", "status": "working", **fields}
        )
    assert store.session(a["agent"]["id"])["report"] == original["report"]


@pytest.mark.parametrize(
    "fields",
    [
        {"limit": True},
        {"limit": 101},
        {"after": 5},
        {"include_stale": "false"},
        {"runtime": "other"},
        {"status": "other"},
    ],
)
def test_invalid_directory_filters(store, fields):
    with pytest.raises(WireError):
        store.sessions(**fields)
