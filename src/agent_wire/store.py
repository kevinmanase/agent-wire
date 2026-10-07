# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal, get_args

from .errors import WireError
from .processes import is_app_server, process_starts

MAX_BODY = 65_536
MAX_PENDING = 64
MAX_HOPS = 8
PENDING = ("queued", "delivering", "submitted", "unknown")
IN_FLIGHT = "(" + ",".join(f"'{status}'" for status in PENDING) + ")"
WORK_STATES = ("working", "waiting", "blocked", "idle", "done")
Role = Literal["main", "lead", "worker"]
AskKind = Literal["decide", "act", "approve"]
ROLES = get_args(Role)
ASK_KINDS = get_args(AskKind)
# Optional report text and its limit in UTF-8 bytes.
REPORT_TEXT = {
    "detail": 2048,
    "repository": 4096,
    "branch": 256,
    "ticket": 256,
    "lane": 64,
    "stage": 64,
}
ASK_FIELDS = ("to", "text", "kind")
ASK_TEXT = 512
# Preset answers an ask may carry, in the asker's order.
REPORT_COLUMNS = {
    "lane": "TEXT NOT NULL DEFAULT ''",
    "stage": "TEXT NOT NULL DEFAULT ''",
    "role": "TEXT",
    "ask_to": "TEXT",
    "ask_text": "TEXT",
    "ask_kind": "TEXT",
    "ask_options": "TEXT",
    "ask_raised_at": "REAL",
    "ask_native": "INTEGER",
    "ask_answer": "TEXT",
    # The latest raised_at this session's asks ever had; a new ask's is always later.
    "ask_last_raised": "REAL",
}
MAX_ANSWER = 4096
STALE_AFTER = 300
# Linux derives a process's start time from a wall-clock boot time, which jitters and moves
# when the clock steps. A start this far from the recorded one means a reused pid.
START_SLACK = 60
# Other active enrollments of a runtime in a process: (runtime, id, pid, started, slack).
SAME_PROCESS = "active=1 AND runtime=? AND id!=? AND pid=? AND abs(started-?)<=?"
FOLD_AFTER = 6 * 3600
# A finished entry: done, no contact for FOLD_AFTER, no open ask, and no message in flight.
FINISHED = (
    "COALESCE(r.status,'') = 'done' AND r.last_seen < ? AND r.ask_kind IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM messages m WHERE (m.sender=a.id OR m.recipient=a.id) "
    f"AND m.status IN {IN_FLIGHT})"
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bounded_text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise WireError(
            "invalid_input", f"{field} must be nonempty text, at most {limit} UTF-8 bytes"
        )
    return value


def check_ask(ask):
    # An ask is the agent's claim that it needs a person; the broker never acts on it.
    if ask is None:
        return
    if not isinstance(ask, dict) or set(ask) - {"options", "native"} != set(ASK_FIELDS):
        raise WireError(
            "invalid_input",
            "ask must be null or an object of to, text, kind, and optional options and native",
        )
    if type(ask.get("native", False)) is not bool:
        raise WireError("invalid_input", "ask.native must be a boolean")
    bounded_text(ask["to"], "ask.to", 64)
    bounded_text(ask["text"], "ask.text", ASK_TEXT)
    if ask["kind"] not in ASK_KINDS:
        raise WireError("invalid_input", f"ask.kind must be one of {ASK_KINDS}")
    # Preset answers in the asker's order, limited in characters as the shared contract says.
    options = ask.get("options")
    if options is None:
        return
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise WireError("invalid_input", "ask.options must be a list of 2 to 4 answers")
    for option in options:
        if not isinstance(option, str) or not option.strip() or len(option) > 80:
            raise WireError(
                "invalid_input", "each ask option must be nonempty text, at most 80 characters"
            )


def check_raised_at(value):
    if type(value) not in (int, float):
        raise WireError("invalid_input", "raised_at must be the ask's raised_at number")


def ask_columns(ask: dict | None, now: float) -> dict:
    """The ask's column values, under the names raised_at() reads."""
    return {
        **{f"ask_{key}": ask and ask[key] for key in ASK_FIELDS},
        "ask_options": ask and ask.get("options") and json.dumps(ask["options"]),
        "ask_native": 1 if ask and ask.get("native") else None,
        "ask_raised_at": now if ask else None,
    }


def raised_at(new: str) -> str:
    """SQL for ask_raised_at, given the prefix that names the ask_columns() values.

    SET expressions read the previous row, so an unchanged ask keeps its raised_at. A native
    ask is a newly opened dialog each time: it always starts over, so it never inherits an
    earlier dialog's answer. A new ask's raised_at is later than every earlier one of the
    session, even if the clock repeats or steps back, so an answer matched on it can't
    reach another ask.
    """
    return (
        f"CASE WHEN {new}ask_kind IS NULL THEN NULL "
        f"WHEN ask_raised_at IS NOT NULL AND ask_to IS {new}ask_to "
        f"AND ask_text IS {new}ask_text AND ask_kind IS {new}ask_kind "
        f"AND ask_options IS {new}ask_options AND {new}ask_native IS NULL "
        f"AND ask_native IS NULL THEN ask_raised_at "
        f"ELSE MAX({new}ask_raised_at, COALESCE(ask_last_raised, ask_raised_at, 0) + 1e-6) END"
    )


def last_raised(new: str) -> str:
    """SQL for ask_last_raised: the new raised_at, or the previous latest when cleared."""
    return f"COALESCE(({raised_at(new)}), ask_last_raised)"


def kept_answer(new: str) -> str:
    """SQL for ask_answer: an answer belongs to one raised ask and goes when that ask does."""
    return f"CASE WHEN ({raised_at(new)}) IS ask_raised_at THEN ask_answer END"


class Store:
    def __init__(
        self, path: Path, clock=time.time, processes=process_starts, app_server=is_app_server
    ):
        self.clock = clock
        self.processes = processes
        self.app_server = app_server
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(f"""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL,
                native_id TEXT NOT NULL, endpoint TEXT NOT NULL, cwd TEXT NOT NULL,
                credential TEXT NOT NULL UNIQUE, active INTEGER NOT NULL, created REAL NOT NULL,
                mode TEXT
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
            CREATE INDEX IF NOT EXISTS pending_from ON messages(sender)
                WHERE status IN {IN_FLIGHT};
            CREATE INDEX IF NOT EXISTS pending_to ON messages(recipient)
                WHERE status IN {IN_FLIGHT};
            CREATE TABLE IF NOT EXISTS session_reports (
                agent_id TEXT PRIMARY KEY REFERENCES agents(id),
                task TEXT NOT NULL DEFAULT '', status TEXT,
                detail TEXT NOT NULL DEFAULT '', repository TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '', ticket TEXT NOT NULL DEFAULT '',
                reported_at REAL, last_seen REAL NOT NULL,
                activity TEXT NOT NULL DEFAULT 'unknown',
                needs_update INTEGER NOT NULL DEFAULT 1
            );
        """)
        # The runtime's process and its start time; a reused pid has a different start.
        self.add_missing_columns("agents", {"mode": "TEXT", "pid": "INTEGER", "started": "REAL"})
        self.add_missing_columns("session_reports", REPORT_COLUMNS)
        self.db.execute(
            "UPDATE messages SET status='unknown', detail='Broker restarted during delivery' "
            "WHERE status='delivering'"
        )

    def add_missing_columns(self, table: str, columns: dict[str, str]):
        # Databases from older releases gain new columns in place.
        present = {c["name"] for c in self.db.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in present:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self):
        self.db.close()

    @staticmethod
    def public_agent(row) -> dict:
        return {key: row[key] for key in ("id", "name", "runtime", "native_id", "cwd", "active")}

    def process(self, pid) -> tuple:
        """The (pid, start time) to record, or (None, None) when it can't be read."""
        if pid is not None and (type(pid) is not int or pid < 1):
            raise WireError("invalid_input", "pid must be a positive integer or null")
        started = pid and self.processes([pid]).get(pid)
        return (pid, started) if started else (None, None)

    def replaces(self, agent_id: str, runtime: str, process: tuple) -> bool:
        """Whether this session ends an earlier one in its process, after /clear or /resume.

        Claude Code runs one conversation per process and terminal Codex one thread; a Codex
        app server runs many. Reads ps only when an earlier one exists.
        """
        if (
            runtime not in ("claude", "codex")
            or not self.db.execute(
                f"SELECT 1 FROM agents WHERE {SAME_PROCESS}",
                (runtime, agent_id, *process, START_SLACK),
            ).fetchone()
        ):
            return False
        return runtime == "claude" or not self.app_server(process[0])

    def retire_replaced(self, agent_id: str, runtime: str, process: tuple):
        """Retire other enrollments of this runtime in this one's process, as retire does."""
        self.db.execute(
            f"UPDATE agents SET active=0 WHERE {SAME_PROCESS}",
            (runtime, agent_id, *process, START_SLACK),
        )

    def register(
        self, name: str, runtime: str, native_id: str, endpoint: dict, cwd="", pid=None
    ) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
            raise WireError("invalid_name", "Name must contain 1–64 letters, digits, dots, _ or -")
        if name.lower() == "ready":
            raise WireError("invalid_name", "A cleared session named ready is not a recipient")
        bounded_text(native_id, "native_id", 200)
        if not isinstance(cwd, str) or len(cwd.encode()) > 4096:
            raise WireError("invalid_input", "cwd must be text, at most 4096 UTF-8 bytes")
        if runtime not in ("codex", "claude", "mailbox"):
            raise WireError("invalid_runtime", "Runtime must be codex, claude, or mailbox")
        process = self.process(pid)
        # An exited session's name is free, whether or not anything listed sessions since.
        self.retire_exited([name])
        token = secrets.token_urlsafe(32)
        agent_id = str(uuid.uuid4())
        replaces = self.replaces(agent_id, runtime, process)  # Can read ps: before the lock.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Retire before the name check, so a replacement can take its predecessor's name.
            self.db.execute(
                "UPDATE agents SET active=0 WHERE runtime=? AND native_id=? AND active=1",
                (runtime, native_id),
            )
            if replaces:
                self.retire_replaced(agent_id, runtime, process)
            if self.db.execute(
                "SELECT 1 FROM agents WHERE name=? AND active=1", (name,)
            ).fetchone():
                raise WireError("name_in_use", "An active session already owns that name")
            self.db.execute(
                "INSERT INTO agents (id,name,runtime,native_id,endpoint,cwd,credential,active,"
                "created,pid,started) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    *process,
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

    def retire_exited(self, targets: list[str] | None = None):
        """Retire active sessions whose process has exited, exactly as retire does.

        Checks every session, or only those named by id or name in targets. A session
        without a recorded process, or whose start time can't be read, stays.
        """
        query = "SELECT id,pid,started FROM agents WHERE active=1 AND pid IS NOT NULL"
        if targets is not None:
            marks = ",".join("?" * len(targets))
            query += f" AND (id IN ({marks}) OR name IN ({marks}))"
        rows = self.db.execute(query, [*(targets or ()), *(targets or ())]).fetchall()
        starts = self.processes([row["pid"] for row in rows])
        self.db.executemany(
            "UPDATE agents SET active=0 WHERE id=?",
            [
                (row["id"],)
                for row in rows
                if row["pid"] not in starts
                or abs((starts[row["pid"]] or row["started"]) - row["started"]) > START_SLACK
            ],
        )

    def agents(self) -> list[dict]:
        self.retire_exited()
        return [
            self.public_agent(r)
            for r in self.db.execute("SELECT * FROM agents WHERE active=1 ORDER BY name")
        ]

    def session_update(
        self,
        token: str,
        *,
        task: str,
        status: str,
        detail: str = "",
        repository: str = "",
        branch: str = "",
        ticket: str = "",
        lane: str = "",
        stage: str = "",
        role: str | None = None,
        ask: dict | None = None,
    ) -> dict:
        """Replace the caller's report. Hooks never infer or overwrite these fields."""
        agent = self.authenticate(token)
        bounded_text(task, "task", 512)
        if status not in WORK_STATES:
            raise WireError("invalid_input", f"status must be one of {WORK_STATES}")
        text = dict(
            detail=detail,
            repository=repository,
            branch=branch,
            ticket=ticket,
            lane=lane,
            stage=stage,
        )
        for name, value in text.items():
            limit = REPORT_TEXT[name]
            if not isinstance(value, str) or len(value.encode()) > limit:
                raise WireError(
                    "invalid_input", f"{name} must be text, at most {limit} UTF-8 bytes"
                )
        if role is not None and role not in ROLES:
            raise WireError("invalid_input", f"role must be one of {ROLES} or null")
        check_ask(ask)
        now = self.clock()
        row = {
            "task": task,
            "status": status,
            **text,
            "role": role,
            **ask_columns(ask, now),
            "reported_at": now,
            "last_seen": now,
        }
        row["ask_last_raised"] = row["ask_raised_at"]
        derived = {"ask_raised_at", "ask_last_raised"}
        self.db.execute(
            f"INSERT INTO session_reports (agent_id,{','.join(row)},needs_update) "
            f"VALUES (:agent_id,{','.join(':' + key for key in row)},0) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            + ",".join(f"{key}=excluded.{key}" for key in row if key not in derived)
            + ",needs_update=0,ask_raised_at="
            + raised_at("excluded.")
            + ",ask_last_raised="
            + last_raised("excluded.")
            + ",ask_answer="
            + kept_answer("excluded."),
            {**row, "agent_id": agent["id"]},
        )
        return self.session(agent["id"])

    def session_ask(
        self, token: str, *, ask: dict | None, if_raised_at: float | None = None
    ) -> dict:
        """Set or clear (null) only the caller's ask; the rest of its report stays.

        With `if_raised_at`, only while the open ask is the one raised then.
        """
        agent = self.authenticate(token)
        check_ask(ask)
        if if_raised_at is not None:
            check_raised_at(if_raised_at)
        now = self.clock()
        columns = ask_columns(ask, now)
        changed = self.db.execute(
            f"UPDATE session_reports SET ask_raised_at={raised_at(':')},"
            f"ask_last_raised={last_raised(':')},ask_answer={kept_answer(':')},"
            + "".join(f"{key}=:{key}," for key in columns if key != "ask_raised_at")
            + "last_seen=:now WHERE agent_id=:agent_id AND reported_at IS NOT NULL"
            + (" AND ask_raised_at=:if_raised_at" if if_raised_at is not None else ""),
            {**columns, "now": now, "agent_id": agent["id"], "if_raised_at": if_raised_at},
        ).rowcount
        if not changed and ask is not None and if_raised_at is None:
            raise WireError("no_report", "Publish a report before setting an ask")
        return self.session(agent["id"])

    def answer(self, session: str, raised_at: float, answer: str) -> dict:
        """Record a person's answer to a session's open native-dialog ask.

        Takes no session credential: the broker accepts it only from the local user. It lands
        only on the ask raised at `raised_at`, so a stale answer never reaches a newer question.
        """
        bounded_text(answer, "answer", MAX_ANSWER)
        check_raised_at(raised_at)
        bounded_text(session, "session", 200)
        agent = self.resolve(session)
        if self.db.execute(
            "UPDATE session_reports SET ask_answer=? WHERE agent_id=? AND ask_raised_at=? "
            "AND ask_native=1 AND ask_answer IS NULL",
            (answer, agent["id"], raised_at),
        ).rowcount:
            return {"session": agent["name"], "raised_at": raised_at, "answered": True}
        row = self.db.execute(
            "SELECT ask_raised_at,ask_native FROM session_reports WHERE agent_id=?",
            (agent["id"],),
        ).fetchone()
        if row is None or row["ask_raised_at"] != raised_at:
            raise WireError("ask_closed", "That ask was answered, cleared, or replaced")
        if not row["ask_native"]:
            raise WireError(
                "not_native", "That ask is not a dialog Agent Wire can answer; reply in the session"
            )
        raise WireError("already_answered", "That ask already has an answer")

    def ask_poll(self, token: str, *, raised_at: float) -> dict:
        """Whether the caller's ask raised at `raised_at` is open, and its answer, taken once.

        Taking an answer clears that ask.
        """
        agent = self.authenticate(token)
        check_raised_at(raised_at)
        row = self.db.execute(
            "SELECT ask_answer FROM session_reports WHERE agent_id=? AND ask_raised_at=?",
            (agent["id"], raised_at),
        ).fetchone()
        if row is None:
            return {"open": False, "answer": None}
        if row["ask_answer"] is None:
            return {"open": True, "answer": None}
        self.session_ask(token, ask=None, if_raised_at=raised_at)
        return {"open": False, "answer": row["ask_answer"]}

    def refresh_endpoint(self, token: str, endpoint: dict, cwd: str, pid=None) -> dict:
        # Only called after native validation by the broker; recheck after that async work.
        agent = self.authenticate(token)
        if not isinstance(cwd, str) or len(cwd.encode()) > 4096:
            raise WireError("invalid_input", "cwd must be text, at most 4096 UTF-8 bytes")
        # A resumed conversation can run in a new process.
        process = self.process(pid)
        if process == (None, None) and pid in (None, agent["pid"]):
            # Unknown or unreadable, not proof of a new process: keep the recorded one for the
            # exit check. A Codex hook sends no pid when its own ps fails.
            process = (agent["pid"], agent["started"])
        self.db.execute(
            "UPDATE agents SET endpoint=?,cwd=?,pid=?,started=? WHERE id=?",
            (json.dumps(endpoint), cwd, *process, agent["id"]),
        )
        if self.replaces(agent["id"], agent["runtime"], process):
            self.retire_replaced(agent["id"], agent["runtime"], process)
        return self.public_agent(self.agent(agent["id"]))

    def session_heartbeat(
        self, token: str, *, activity: str, new_turn: bool = False, mode: str | None = None
    ) -> dict:
        agent = self.authenticate(token)
        if (
            activity not in ("working", "waiting", "idle")
            or type(new_turn) is not bool
            or mode not in (None, "bypass", "prompting")
        ):
            raise WireError(
                "invalid_input",
                "Expected activity working/waiting/idle, boolean new_turn, "
                "and mode bypass/prompting/null",
            )
        # The latest hook's permission class; a hook without one clears it rather than go stale.
        self.db.execute("UPDATE agents SET mode=? WHERE id=?", (mode, agent["id"]))
        self.db.execute(
            "INSERT INTO session_reports (agent_id,last_seen,activity,needs_update) "
            "VALUES (?,?,?,1) "
            "ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen,"
            "activity=excluded.activity,needs_update=CASE WHEN ? THEN 1 ELSE needs_update END",
            (agent["id"], self.clock(), activity, new_turn),
        )
        return self.session(agent["id"])

    @staticmethod
    def session_view(row, now: float) -> dict:
        result = Store.public_agent(row)
        seen = row["last_seen"]
        result.update(
            last_seen=seen,
            age_seconds=max(0, now - seen) if seen is not None else None,
            freshness="unseen"
            if seen is None
            else "stale"
            if now - seen >= STALE_AFTER
            else "fresh",
            activity=row["activity"] or "unknown",
            report=None,
        )
        if row["reported_at"] is not None:
            result["report"] = {
                key: row[key] for key in ("task", "status", *REPORT_TEXT, "role", "reported_at")
            }
            result["report"]["ask"] = row["ask_kind"] and {
                key: row[f"ask_{key}"] for key in (*ASK_FIELDS, "raised_at")
            }
            if row["ask_options"]:
                result["report"]["ask"]["options"] = json.loads(row["ask_options"])
            if row["ask_native"]:
                result["report"]["ask"]["native"] = True
            result["report"]["needs_update"] = bool(row["needs_update"])
        return result

    def session(self, agent_id: str) -> dict:
        row = self.db.execute(
            "SELECT a.*,r.* FROM agents a LEFT JOIN session_reports r ON r.agent_id=a.id "
            "WHERE a.id=?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise WireError("not_found", "Unknown session")
        return self.session_view(row, self.clock())

    def sessions(
        self,
        *,
        runtime: str | None = None,
        status: str | None = None,
        include_stale: bool = True,
        include_finished: bool = False,
        after: str = "",
        limit: int = 50,
    ) -> dict:
        """List active enrollments. Folding finished entries is a display filter only."""
        if runtime is not None and runtime not in ("codex", "claude", "mailbox"):
            raise WireError("invalid_input", "Invalid runtime filter")
        if status is not None and status not in (*WORK_STATES, "unreported"):
            raise WireError("invalid_input", "Invalid status filter")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(include_stale) is not bool
            or type(include_finished) is not bool
        ):
            raise WireError(
                "invalid_input",
                "limit must be 1–100; include_stale and include_finished must be boolean",
            )
        if not isinstance(after, str) or len(after) > 64:
            raise WireError("invalid_input", "Invalid session cursor")
        self.retire_exited()
        now = self.clock()
        clauses, params = ["a.active=1"], []
        if runtime is not None:
            clauses.append("a.runtime=?")
            params.append(runtime)
        if status is not None:
            clauses.append("COALESCE(r.status,'unreported')=?")
            params.append(status)
        if not include_stale:
            clauses.append("r.last_seen>?")
            params.append(now - STALE_AFTER)
        query = (
            "FROM agents a LEFT JOIN session_reports r ON r.agent_id=a.id WHERE "
            + " AND ".join(clauses)
        )
        hidden = 0
        if not include_finished:
            # Count across all pages so every page reports the same total.
            params.append(now - FOLD_AFTER)
            hidden = self.db.execute(
                f"SELECT COUNT(*) {query} AND ({FINISHED})", params
            ).fetchone()[0]
            query += f" AND NOT ({FINISHED})"
        rows = self.db.execute(
            f"SELECT a.*,r.* {query} AND a.id>? ORDER BY a.id LIMIT ?",
            [*params, after, limit + 1],
        )
        sessions, size, more = [], 0, False
        for row in rows:
            item = self.session_view(row, now)
            encoded = len(json.dumps(item).encode())
            if len(sessions) == limit or size + encoded > 512 * 1024:
                more = True
                break
            sessions.append(item)
            size += encoded
        return {
            "sessions": sessions,
            "next_after": sessions[-1]["id"] if more else None,
            "generated_at": now,
            "stale_after_seconds": STALE_AFTER,
            "hidden_finished": hidden,
        }

    def resolve(self, target: str):
        self.retire_exited([target])
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
            f"SELECT COUNT(*) FROM messages WHERE recipient=? AND status IN {IN_FLIGHT}",
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
            f"AND status IN {IN_FLIGHT} ORDER BY seq LIMIT ?",
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
