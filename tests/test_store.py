# SPDX-License-Identifier: AGPL-3.0-only
import json
import uuid

import pytest

from agent_wire.errors import WireError
from agent_wire.store import MAX_HOPS, MAX_PENDING, Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "messages.sqlite3")
    yield db
    db.close()


def enroll(store, name="a", runtime="mailbox"):
    return store.register(name, runtime, str(uuid.uuid4()), {}, "")


def send(store, sender, recipient, body="hello", **kwargs):
    return store.send(
        sender["session_handle"],
        recipient["agent"]["id"],
        body,
        idempotency_key=kwargs.pop("idempotency_key", str(uuid.uuid4())),
        **kwargs,
    )


def test_inbox_receipt_and_reply(store):
    a, b = enroll(store), enroll(store, "b")
    msg = send(store, a, b)
    assert msg["status"] == "queued"
    inbox = store.inbox(b["session_handle"])
    assert [m["id"] for m in inbox["messages"]] == [msg["id"]]
    assert store.status(a["session_handle"], msg["id"])["status"] == "queued"
    assert "credential" not in str(inbox)
    assert a["session_handle"] not in str(inbox)
    store.ack(b["session_handle"], msg["id"])
    assert store.inbox(b["session_handle"])["messages"] == []
    reply = send(store, b, a, "received", in_reply_to=msg["id"])
    state = store.status(a["session_handle"], msg["id"])
    assert state["status"] == "replied"
    assert state["reply_id"] == reply["id"]
    store.ack(b["session_handle"], msg["id"])
    assert store.status(a["session_handle"], msg["id"])["status"] == "replied"


def test_message_access_is_limited_to_participants(store):
    a, b, stranger = enroll(store), enroll(store, "b"), enroll(store, "stranger")
    msg = send(store, a, b)
    for token in (a["session_handle"], stranger["session_handle"]):
        with pytest.raises(WireError, match="Only the recipient"):
            store.ack(token, msg["id"])
    with pytest.raises(WireError, match="Only participants"):
        store.status(stranger["session_handle"], msg["id"])
    with pytest.raises(WireError, match="Invalid or retired"):
        store.inbox("wrong")


def test_idempotency_is_content_checked(store):
    a, b = enroll(store), enroll(store, "b")
    first = send(store, a, b, idempotency_key="same")
    again = send(store, a, b, idempotency_key="same")
    assert first["id"] == again["id"]
    with pytest.raises(WireError, match="different message"):
        send(store, a, b, "changed", idempotency_key="same")
    assert len(store.inbox(b["session_handle"])["messages"]) == 1


def test_reregistration_revokes_old_identity_and_address(store):
    a = enroll(store)
    native = a["agent"]["native_id"]
    replacement = store.register("a", "mailbox", native, {})
    assert replacement["agent"]["id"] != a["agent"]["id"]
    with pytest.raises(WireError, match="Invalid or retired"):
        store.authenticate(a["session_handle"])
    with pytest.raises(WireError, match="unknown, retired"):
        store.resolve(a["agent"]["id"])
    assert store.resolve("a")["id"] == replacement["agent"]["id"]


def test_name_collision_does_not_retire_existing_agent(store):
    a = enroll(store)
    with pytest.raises(WireError, match="already owns"):
        enroll(store)
    assert store.authenticate(a["session_handle"])["active"] == 1


def test_expiry_is_not_acknowledgement(store):
    clock = [1000.0]
    store.clock = lambda: clock[0]
    a, b = enroll(store), enroll(store, "b")
    msg = send(store, a, b, ttl=1)
    clock[0] += 2
    assert store.inbox(b["session_handle"])["messages"] == []
    assert store.status(a["session_handle"], msg["id"])["status"] == "expired"
    with pytest.raises(WireError, match="no longer awaiting"):
        store.ack(b["session_handle"], msg["id"])


def test_reply_route_and_hop_limits(store):
    a, b, c = enroll(store), enroll(store, "b"), enroll(store, "c")
    msg = send(store, a, b)
    with pytest.raises(WireError, match="Replies must"):
        send(store, b, c, in_reply_to=msg["id"])
    for _ in range(MAX_HOPS):
        a, b = b, a
        msg = send(store, a, b, in_reply_to=msg["id"])
    with pytest.raises(WireError, match="Reply chain limit"):
        send(store, b, a, in_reply_to=msg["id"])


def test_backpressure(store):
    clock = [1000.0]
    store.clock = lambda: clock[0]
    a, b = enroll(store), enroll(store, "b")
    for _ in range(MAX_PENDING):
        clock[0] += 61
        send(store, a, b, ttl=86400)
    with pytest.raises(WireError, match="inbox is full"):
        send(store, a, b)


def test_sender_rate_limit(store):
    a, b = enroll(store), enroll(store, "b")
    for _ in range(30):
        send(store, a, b)
    with pytest.raises(WireError, match="30 per minute"):
        send(store, a, b)


def test_large_inbox_pages_preserve_cursor_and_fit_wire_frame(store):
    a, b = enroll(store), enroll(store, "b")
    ids = [send(store, a, b, "x" + "\x00" * 65535)["id"] for _ in range(5)]
    cursor, seen = 0, []
    while True:
        page = store.inbox(b["session_handle"], after=cursor)
        assert len(json.dumps({"result": page}).encode()) < 1_048_576
        if not page["messages"]:
            break
        seen.extend(m["id"] for m in page["messages"])
        assert page["cursor"] > cursor
        cursor = page["cursor"]
    assert seen == ids


def test_retired_mailbox_closes_queued_messages(store):
    a, b = enroll(store), enroll(store, "b")
    message = send(store, a, b)
    store.retire(b["session_handle"])
    assert store.status(a["session_handle"], message["id"])["status"] == "failed"


@pytest.mark.parametrize("body", ["", " ", "é" * 32769, None, {"body": "wrong"}])
def test_invalid_bodies(store, body):
    with pytest.raises(WireError, match="body must"):
        send(store, enroll(store), enroll(store, "b"), body)


@pytest.mark.parametrize("ttl", [0, -1, 86401, True, "10"])
def test_invalid_ttl(store, ttl):
    with pytest.raises(WireError, match="ttl must"):
        send(store, enroll(store), enroll(store, "b"), ttl=ttl)


def test_restart_preserves_messages_without_replaying_uncertain_delivery(tmp_path):
    path = tmp_path / "db"
    first = Store(path)
    a, b = enroll(first), enroll(first, "b")
    message = send(first, a, b)
    first.transition(message["id"], "delivering", expected="queued")
    first.close()
    second = Store(path)
    try:
        assert second.status(a["session_handle"], message["id"])["status"] == "unknown"
        assert second.next_delivery() == []
        second.ack(b["session_handle"], message["id"])
        assert second.status(a["session_handle"], message["id"])["status"] == "acknowledged"
    finally:
        second.close()
