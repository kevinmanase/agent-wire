# SPDX-License-Identifier: AGPL-3.0-only
import json
import sqlite3

import pytest

from agent_wire.broker import Broker
from agent_wire.errors import WireError
from agent_wire.store import FOLD_AFTER, STALE_AFTER, Store

from .test_store import enroll, send


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "db")
    yield db
    db.close()


@pytest.fixture
def now(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    return now


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
        {"include_finished": 1},
        {"runtime": "other"},
        {"status": "other"},
    ],
)
def test_invalid_directory_filters(store, fields):
    with pytest.raises(WireError):
        store.sessions(**fields)


ASK = {"to": "kevin", "text": "Merge api before mobile?", "kind": "decide"}
CLEARED = ("", "", None, None)


def new_fields(report):
    return report["lane"], report["stage"], report["role"], report["ask"]


def test_lane_stage_role_and_ask_are_reported(store, now):
    a = enroll(store)
    report = store.session_update(
        a["session_handle"],
        task="Ship API",
        status="waiting",
        lane="api",
        stage="REVIEW",
        role="lead",
        ask=ASK,
    )["report"]
    assert report["lane"] == "api"
    assert report["stage"] == "REVIEW"
    assert report["role"] == "lead"
    assert report["ask"] == {**ASK, "raised_at": 1000.0}
    assert store.sessions()["sessions"][0]["report"] == report


def test_ask_raised_at_is_kept_while_the_same_ask_stays(store, now):
    token = enroll(store)["session_handle"]

    def update(**fields):
        now[0] += 60
        return store.session_update(token, task="Ship API", status="waiting", **fields)["report"]

    assert update(ask=ASK)["ask"]["raised_at"] == 1060
    # Other fields may change while the ask stays open.
    held = update(ask=ASK, stage="QA", detail="Still waiting")
    assert held["ask"]["raised_at"] == 1060
    assert held["reported_at"] == 1120
    store.session_heartbeat(token, activity="working", new_turn=True)
    assert update(ask=ASK)["ask"]["raised_at"] == 1060
    for changed in ({"to": "lead"}, {"text": "Merge mobile first?"}, {"kind": "approve"}):
        assert update(ask={**ASK, **changed})["ask"]["raised_at"] == now[0]
    raised = now[0]
    assert update(ask={**ASK, "kind": "approve"})["ask"]["raised_at"] == raised
    # Clearing and raising the same ask again starts a new ask.
    assert update()["ask"] is None
    assert update(ask=ASK)["ask"]["raised_at"] == now[0]


def test_omitted_report_fields_are_cleared(store):
    a = enroll(store)
    full = {"lane": "api", "stage": "REVIEW", "role": "worker", "ask": ASK}
    store.session_update(a["session_handle"], task="Ship API", status="waiting", **full)
    report = store.session_update(a["session_handle"], task="Ship API", status="working")["report"]
    assert new_fields(report) == CLEARED


def test_old_clients_and_databases_keep_working(tmp_path):
    path = tmp_path / "db"
    # The report table as released before lane, stage, role, and ask existed.
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE session_reports (
            agent_id TEXT PRIMARY KEY, task TEXT NOT NULL DEFAULT '', status TEXT,
            detail TEXT NOT NULL DEFAULT '', repository TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '', ticket TEXT NOT NULL DEFAULT '',
            reported_at REAL, last_seen REAL NOT NULL,
            activity TEXT NOT NULL DEFAULT 'unknown', needs_update INTEGER NOT NULL DEFAULT 1
        );
    """)
    old.close()
    store = Store(path)
    try:
        a = enroll(store)
        report = store.session_update(
            a["session_handle"], task="Old client", status="working", ticket="T-1"
        )["report"]
        assert report["ticket"] == "T-1"
        assert new_fields(report) == CLEARED
    finally:
        store.close()


def test_report_fields_survive_restart(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    a = enroll(store)
    before = store.session_update(
        a["session_handle"], task="Ship API", status="waiting", lane="api", role="main", ask=ASK
    )["report"]
    store.close()
    store = Store(path)
    try:
        assert store.sessions()["sessions"][0]["report"] == before
        held = store.session_update(
            a["session_handle"], task="Ship API", status="waiting", lane="api", ask=ASK
        )["report"]
        assert held["ask"]["raised_at"] == before["ask"]["raised_at"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"lane": "x" * 65},
        {"lane": "é" * 33},
        {"lane": None},
        {"stage": "x" * 65},
        {"role": "boss"},
        {"role": ""},
        {"ask": "Merge?"},
        {"ask": {}},
        {"ask": {"to": "kevin", "text": "Merge?"}},
        {"ask": {**ASK, "raised_at": 1}},
        {"ask": {**ASK, "to": ""}},
        {"ask": {**ASK, "to": "x" * 65}},
        {"ask": {**ASK, "text": " "}},
        {"ask": {**ASK, "text": "x" * 513}},
        {"ask": {**ASK, "text": 5}},
        {"ask": {**ASK, "kind": "merge"}},
    ],
)
def test_invalid_report_fields_leave_previous_report_unchanged(store, fields):
    a = enroll(store)
    original = store.session_update(
        a["session_handle"], task="Valid", status="working", lane="api", ask=ASK
    )
    with pytest.raises(WireError, match="must be"):
        store.session_update(a["session_handle"], task="Valid", status="working", **fields)
    assert store.session(a["agent"]["id"])["report"] == original["report"]


def test_report_fields_at_their_bounds(store):
    a = enroll(store)
    ask = {"to": "x" * 64, "text": "é" * 256, "kind": "act"}
    report = store.session_update(
        a["session_handle"], task="Edge", status="working", lane="é" * 32, stage="x" * 64, ask=ask
    )["report"]
    assert report["lane"] == "é" * 32
    assert report["ask"]["text"] == "é" * 256


def names(page):
    return {session["name"] for session in page["sessions"]}


def test_finished_sessions_fold_out_of_the_default_list(store, now):
    finished = enroll(store, "finished")
    store.session_update(finished["session_handle"], task="Shipped", status="done")
    recent = enroll(store, "recent")
    blocked = enroll(store, "blocked")
    store.session_update(blocked["session_handle"], task="Need access", status="blocked")
    asking = enroll(store, "asking")
    store.session_update(asking["session_handle"], task="Shipped", status="done", ask=ASK)
    unreported = enroll(store, "unreported")
    store.session_heartbeat(unreported["session_handle"], activity="idle")

    now[0] += FOLD_AFTER
    store.session_update(recent["session_handle"], task="Shipped", status="done")
    # Exactly six hours without contact is not yet more than six hours.
    assert store.sessions()["hidden_finished"] == 0
    now[0] += 1
    page = store.sessions()
    assert names(page) == {"recent", "blocked", "asking", "unreported"}
    assert page["hidden_finished"] == 1
    everything = store.sessions(include_finished=True)
    assert len(everything["sessions"]) == 5
    assert everything["hidden_finished"] == 0
    # Folding is a display filter: the enrollment and report stay intact.
    assert store.authenticate(finished["session_handle"])["active"] == 1
    assert store.session(finished["agent"]["id"])["report"]["status"] == "done"
    # A heartbeat counts as contact and brings the entry back.
    store.session_heartbeat(finished["session_handle"], activity="idle")
    assert "finished" in names(store.sessions())


@pytest.mark.parametrize("status", ["queued", "delivering", "submitted", "unknown"])
@pytest.mark.parametrize("direction", ["to", "from"])
def test_messages_in_flight_keep_finished_sessions_visible(store, now, status, direction):
    finished, peer = enroll(store, "finished"), enroll(store, "peer")
    store.session_update(finished["session_handle"], task="Shipped", status="done")
    sender, recipient = (peer, finished) if direction == "to" else (finished, peer)
    message = send(store, sender, recipient, ttl=86400)
    store.db.execute("UPDATE messages SET status=? WHERE id=?", (status, message["id"]))
    now[0] += FOLD_AFTER + 1
    page = store.sessions()
    assert names(page) == {"finished", "peer"}
    assert page["hidden_finished"] == 0
    store.db.execute("UPDATE messages SET status='acknowledged' WHERE id=?", (message["id"],))
    page = store.sessions()
    assert names(page) == {"peer"}
    assert page["hidden_finished"] == 1


def test_hidden_count_respects_filters_and_pages(store, now):
    for i in range(5):
        a = enroll(store, f"done{i}", "codex" if i % 2 else "claude")
        store.session_update(a["session_handle"], task="Shipped", status="done")
    now[0] += FOLD_AFTER + 1
    for i in range(3):
        a = enroll(store, f"live{i}")
        store.session_update(a["session_handle"], task="Working", status="working")
    first = store.sessions(limit=2)
    second = store.sessions(limit=2, after=first["next_after"])
    assert first["hidden_finished"] == second["hidden_finished"] == 5
    assert len(first["sessions"]) + len(second["sessions"]) == 3
    assert second["next_after"] is None
    assert store.sessions(runtime="codex")["hidden_finished"] == 2
    assert store.sessions(status="done")["sessions"] == []
    # Fresh-only listings already leave these entries out as stale.
    assert store.sessions(include_stale=False)["hidden_finished"] == 0
