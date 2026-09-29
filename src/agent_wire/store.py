# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from .errors import WireError

MAX_BODY = 65_536
MAX_PENDING = 64
MAX_HOPS = 8
PENDING = ("queued", "delivering", "submitted", "unknown")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bounded_text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise WireError(
            "invalid_input", f"{field} must be nonempty text, at most {limit} UTF-8 bytes"
        )
    return value


class Store:
    def __init__(self, path: Path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL,
                native_id TEXT NOT NULL, endpoint TEXT NOT NULL, cwd TEXT NOT NULL,
                credential TEXT NOT NULL UNIQUE, active INTEGER NOT NULL, created REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS live_name ON agents(name) WHERE active=1;
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                sender TEXT NOT NULL REFERENCES agents(id),
                recipient TEXT NOT NULL REFERENCES agents(id),
                body TEXT NOT NULL, in_reply_to TEXT REFERENCES messages(id), hops INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL,
                detail TEXT, acknowledged REAL, reply_id TEXT, idempotency_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, UNIQUE(sender,idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS inbox ON messages(recipient,seq);
        """)
        self.db.execute(
            "UPDATE messages SET status='unknown', detail='Broker restarted during delivery' "
            "WHERE status='delivering'"
        )

    def close(self):
        self.db.close()

    @staticmethod
    def public_agent(row) -> dict:
        return {key: row[key] for key in ("id", "name", "runtime", "native_id", "cwd", "active")}

    def register(self, name: str, runtime: str, native_id: str, endpoint: dict, cwd="") -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
            raise WireError("invalid_name", "Name must contain 1–64 letters, digits, dots, _ or -")
        if name.lower() == "ready":
            raise WireError("invalid_name", "A cleared session named ready is not a recipient")
        bounded_text(native_id, "native_id", 200)
        if not isinstance(cwd, str) or len(cwd.encode()) > 4096:
            raise WireError("invalid_input", "cwd must be text, at most 4096 UTF-8 bytes")
        if runtime not in ("codex", "claude", "mailbox"):
            raise WireError("invalid_runtime", "Runtime must be codex, claude, or mailbox")
        token = secrets.token_urlsafe(32)
        agent_id = str(uuid.uuid4())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            clash = self.db.execute(
                "SELECT * FROM agents WHERE name=? AND active=1", (name,)
            ).fetchone()
            if clash and (clash["native_id"], clash["runtime"]) != (native_id, runtime):
                raise WireError("name_in_use", "An active session already owns that name")
            self.db.execute(
                "UPDATE agents SET active=0 WHERE runtime=? AND native_id=? AND active=1",
                (runtime, native_id),
            )
            self.db.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    agent_id,
                    name,
                    runtime,
                    native_id,
                    json.dumps(endpoint),
                    cwd,
                    digest(token),
                    1,
                    self.clock(),
                ),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"agent": self.public_agent(self.agent(agent_id)), "session_handle": token}

    def agent(self, agent_id: str):
        row = self.db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            raise WireError("not_found", "Unknown session")
        return row

    def authenticate(self, token: str):
        if not isinstance(token, str):
            raise WireError("unauthorized", "A session credential is required")
        row = self.db.execute(
            "SELECT * FROM agents WHERE credential=? AND active=1", (digest(token),)
        ).fetchone()
        if not row:
            raise WireError("unauthorized", "Invalid or retired session credential")
        return row

    def agents(self) -> list[dict]:
        return [
            self.public_agent(r)
            for r in self.db.execute("SELECT * FROM agents WHERE active=1 ORDER BY name")
        ]

    def resolve(self, target: str):
        rows = self.db.execute(
            "SELECT * FROM agents WHERE active=1 AND (id=? OR name=?)", (target, target)
        ).fetchall()
        if len(rows) != 1:
            raise WireError("recipient_unavailable", "Recipient is unknown, retired, or ambiguous")
        return rows[0]

    def retire(self, token: str) -> dict:
        agent = self.authenticate(token)
        self.db.execute("UPDATE agents SET active=0 WHERE id=?", (agent["id"],))
        return {"retired": agent["id"]}

    def expire(self):
        self.db.execute(
            "UPDATE messages SET status='expired',"
            "detail='Delivery acknowledgement deadline passed' "
            "WHERE status IN ('queued','submitted','unknown') AND expires<=?",
            (self.clock(),),
        )
        self.db.execute(
            "UPDATE messages SET status='failed',detail='Recipient enrollment retired' "
            "WHERE status='queued' AND recipient IN (SELECT id FROM agents WHERE active=0)"
        )

    def message(self, message_id: str):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise WireError("not_found", "Unknown message")
        return row

    def envelope(self, row) -> dict:
        result = {
            key: row[key]
            for key in (
                "seq",
                "id",
                "body",
                "in_reply_to",
                "hops",
                "created",
                "expires",
                "status",
                "detail",
                "acknowledged",
                "reply_id",
            )
        }
        result["sender"] = self.public_agent(self.agent(row["sender"]))
        result["recipient"] = self.public_agent(self.agent(row["recipient"]))
        return result

    def send(
        self,
        token: str,
        to: str,
        body: str,
        *,
        in_reply_to=None,
        idempotency_key: str,
        ttl: int = 3600,
    ) -> dict:
        sender = self.authenticate(token)
        bounded_text(body, "body", MAX_BODY)
        bounded_text(to, "to", 200)
        bounded_text(idempotency_key, "idempotency_key", 128)
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise WireError("invalid_input", "ttl must be an integer between 1 and 86400 seconds")
        fingerprint = digest(json.dumps([to, body, in_reply_to, ttl]))
        prior = self.db.execute(
            "SELECT * FROM messages WHERE sender=? AND idempotency_key=?",
            (sender["id"], idempotency_key),
        ).fetchone()
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise WireError("idempotency_conflict", "This key was used for a different message")
            return self.envelope(prior)
        self.expire()
        target = self.resolve(to)
        if target["id"] == sender["id"]:
            raise WireError("self_send", "Use another enrolled session as the recipient")
        hops = 0
        if in_reply_to is not None:
            parent = self.message(in_reply_to)
            if parent["recipient"] != sender["id"] or parent["sender"] != target["id"]:
                raise WireError("invalid_reply", "Replies must go from the recipient to the sender")
            hops = parent["hops"] + 1
            if hops > MAX_HOPS:
                raise WireError("hop_limit", "Reply chain limit reached; wait for human direction")
        pending = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient=? "
            "AND status IN ('queued','delivering','submitted','unknown')",
            (target["id"],),
        ).fetchone()[0]
        if pending >= MAX_PENDING:
            raise WireError("inbox_full", "Recipient inbox is full")
        if (
            self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE sender=? AND created>?",
                (sender["id"], self.clock() - 60),
            ).fetchone()[0]
            >= 30
        ):
            raise WireError("rate_limit", "Batch messages; sender limit is 30 per minute")
        message_id, now = str(uuid.uuid4()), self.clock()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO messages (id,sender,recipient,body,in_reply_to,hops,created,expires,"
                "status,idempotency_key,fingerprint) VALUES (?,?,?,?,?,?,?,?,'queued',?,?)",
                (
                    message_id,
                    sender["id"],
                    target["id"],
                    body,
                    in_reply_to,
                    hops,
                    now,
                    now + ttl,
                    idempotency_key,
                    fingerprint,
                ),
            )
            if in_reply_to:
                self.db.execute(
                    "UPDATE messages SET status='replied',reply_id=?,"
                    "acknowledged=COALESCE(acknowledged,?) "
                    "WHERE id=?",
                    (message_id, now, in_reply_to),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.envelope(self.message(message_id))

    def inbox(self, token: str, after: int = 0, limit: int = 50) -> dict:
        recipient = self.authenticate(token)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WireError("invalid_input", "Invalid cursor or limit")
        self.expire()
        rows = self.db.execute(
            "SELECT * FROM messages WHERE recipient=? AND seq>? "
            "AND status IN ('queued','delivering','submitted','unknown') ORDER BY seq LIMIT ?",
            (recipient["id"], after, limit),
        ).fetchall()
        messages, size = [], 0
        for row in rows:
            envelope = self.envelope(row)
            encoded_size = len(json.dumps(envelope).encode()) + 2
            # Include JSON escaping in the budget; a maximal body still fits one page.
            if messages and size + encoded_size > 524_288:
                break
            messages.append(envelope)
            size += encoded_size
        return {"messages": messages, "cursor": messages[-1]["seq"] if messages else after}

    def ack(self, token: str, message_id: str) -> dict:
        recipient = self.authenticate(token)
        row = self.message(message_id)
        if row["recipient"] != recipient["id"]:
            raise WireError("forbidden", "Only the recipient can acknowledge this message")
        self.expire()
        row = self.message(message_id)
        if row["status"] in ("expired", "failed"):
            raise WireError("message_closed", "Message is no longer awaiting acknowledgement")
        self.db.execute(
            "UPDATE messages SET status='acknowledged',acknowledged=?,detail=NULL "
            "WHERE id=? AND status NOT IN ('acknowledged','replied')",
            (self.clock(), message_id),
        )
        return self.envelope(self.message(message_id))

    def status(self, token: str, message_id: str) -> dict:
        agent = self.authenticate(token)
        self.expire()
        row = self.message(message_id)
        if agent["id"] not in (row["sender"], row["recipient"]):
            raise WireError("forbidden", "Only participants can read this message")
        return self.envelope(row)

    def next_delivery(self):
        self.expire()
        return self.db.execute(
            "SELECT m.* FROM messages m JOIN agents a ON a.id=m.recipient "
            "WHERE m.seq IN (SELECT MIN(seq) FROM messages WHERE status='queued' "
            "GROUP BY recipient) AND a.runtime!='mailbox' ORDER BY m.seq LIMIT 32"
        ).fetchall()

    def transition(self, message_id: str, status: str, detail=None, *, expected="delivering"):
        return (
            self.db.execute(
                "UPDATE messages SET status=?,detail=? WHERE id=? AND status=?",
                (status, detail, message_id, expected),
            ).rowcount
            == 1
        )
