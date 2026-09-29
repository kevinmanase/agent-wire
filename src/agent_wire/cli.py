# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from . import __version__
from .adapters import NativeAdapters
from .broker import serve
from .client import call
from .errors import WireError
from .paths import default_state, read_identity, write_identity
from .store import WORK_STATES


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local messaging for Codex and Claude Code")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--state", type=Path, default=default_state(), help="Private broker state directory"
    )
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the local broker in the foreground")
    commands.add_parser("ping", help="Check the local broker")
    discover = commands.add_parser("discover", help="Find native sessions without enrolling them")
    discover.add_argument("--codex-socket")
    register = commands.add_parser("register", help="Enroll one explicitly identified session")
    register.add_argument("--name", required=True)
    register.add_argument("--runtime", choices=["codex", "claude", "mailbox"], required=True)
    register.add_argument(
        "--session", required=True, help="Native session ID; choose any UUID for mailbox"
    )
    register.add_argument(
        "--socket", help="Native runtime Unix socket (required except for mailbox)"
    )
    register.add_argument("--cwd", default="")
    sessions = commands.add_parser("sessions", help="Read the shared self-reported work list")
    sessions.add_argument("--runtime", choices=["codex", "claude", "mailbox"])
    sessions.add_argument("--status", choices=[*WORK_STATES, "unreported"])
    sessions.add_argument(
        "--fresh", action="store_true", help="Only sessions seen in the last 5 minutes"
    )
    sessions.add_argument("--after", default="")
    sessions.add_argument("--limit", type=int, default=50)
    sessions.add_argument("--table", action="store_true", help="Show a readable terminal table")
    for name in ("agents", "inbox", "send", "ack", "status", "retire", "report"):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--identity", type=Path, required=True, help="Private enrollment identity file"
        )
        if name == "report":
            sub.add_argument("--task", required=True)
            sub.add_argument("--status", choices=WORK_STATES, required=True)
            for field in ("detail", "repository", "branch", "ticket"):
                sub.add_argument(f"--{field}", default="")
        if name == "inbox":
            sub.add_argument("--after", type=int, default=0)
            sub.add_argument("--limit", type=int, default=50)
        if name == "send":
            sub.add_argument("--to", required=True, help="Recipient name or enrollment ID")
            content = sub.add_mutually_exclusive_group(required=True)
            content.add_argument("--body")
            content.add_argument("--body-file", type=Path)
            sub.add_argument("--reply-to")
            sub.add_argument("--key", default=None, help="Idempotency key; reuse for a retry")
            sub.add_argument("--ttl", type=int, default=3600)
        if name in ("ack", "status"):
            sub.add_argument("message_id")
    mcp = commands.add_parser("mcp", help="Expose common messaging tools over MCP stdio")
    mcp.add_argument(
        "--identity", type=Path, help="Bind this server to exactly one enrolled session"
    )
    hook = commands.add_parser(
        "hook", help="Enroll/remind/heartbeat from native hook input (opt-in)"
    )
    hook.add_argument("runtime", choices=["codex", "claude"])
    hook.add_argument("--name", help="Default is runtime plus native session ID")
    hook.add_argument("--codex-socket")
    return p


async def run(args):
    state = args.state.absolute()
    if args.command == "serve":
        await serve(state)
        return
    if args.command == "discover":
        return await NativeAdapters().discover(args.codex_socket)
    if args.command == "register":
        endpoint = {"path": args.socket} if args.socket else {}
        result = await call(
            state,
            "register",
            name=args.name,
            runtime=args.runtime,
            native_id=args.session,
            endpoint=endpoint,
            cwd=args.cwd,
        )
        path = write_identity(state, result)
        return {"agent": result["agent"], "identity_file": str(path)}
    if args.command == "hook":
        from .hooks import run_hook

        # Bound delays and fail open if enrollment or the broker is unavailable.
        return await asyncio.wait_for(
            run_hook(
                state,
                args.runtime,
                json.load(sys.stdin),
                name=args.name,
                codex_socket=args.codex_socket,
            ),
            timeout=10,
        )
    if args.command == "sessions":
        return await call(
            state,
            "sessions_list",
            runtime=args.runtime,
            status=args.status,
            include_stale=not args.fresh,
            after=args.after,
            limit=args.limit,
        )
    if args.command == "ping":
        return await call(state, "ping")
    token = read_identity(args.identity)
    if args.command == "report":
        return await call(
            state,
            "session_update",
            session_handle=token,
            **{
                key: getattr(args, key)
                for key in ("task", "status", "detail", "repository", "branch", "ticket")
            },
        )
    if args.command == "agents":
        return await call(state, "agents_list", session_handle=token)
    if args.command == "inbox":
        return await call(
            state, "messages_read", session_handle=token, after=args.after, limit=args.limit
        )
    if args.command == "send":
        body = args.body_file.read_text() if args.body_file else args.body
        return await call(
            state,
            "message_send",
            session_handle=token,
            to=args.to,
            body=body,
            in_reply_to=args.reply_to,
            idempotency_key=args.key or str(uuid.uuid4()),
            ttl=args.ttl,
        )
    if args.command in ("ack", "status"):
        return await call(
            state, f"message_{args.command}", session_handle=token, message_id=args.message_id
        )
    if args.command == "retire":
        return await call(state, "retire", session_handle=token)


def sessions_table(result):
    def safe(value):
        # Reports are peer data: escape terminal control characters, including ESC.
        return json.dumps(value, ensure_ascii=True)[1:-1]

    columns = [("SESSION", 32), ("RUNTIME", 7), ("STATUS", 11), ("SEEN", 9), ("FRESHNESS", 9)]
    lines = ["  ".join(label.ljust(width) for label, width in columns) + "  TASK"]
    for session in result["sessions"]:
        report = session["report"] or {}
        status = report.get("status", "unreported") + ("*" if report.get("needs_update") else "")
        age = session["age_seconds"]
        seen = "never" if age is None else f"{int(age)}s ago"
        values = [session["name"], session["runtime"], status, seen, session["freshness"]]
        line = "  ".join(
            safe(v)[:width].ljust(width) for v, (_, width) in zip(values, columns, strict=True)
        )
        lines.append(line + "  " + safe(report.get("task", "No report yet"))[:100])
    if not result["sessions"]:
        lines.append("No matching enrolled sessions.")
    lines.append("\n* Report predates the latest prompt. Stale means no contact for 5 minutes.")
    if result["next_after"]:
        lines.append(f"More entries: use --after {result['next_after']}")
    return "\n".join(lines)


def main():
    args = parser().parse_args()
    try:
        if args.command == "mcp":
            from .mcp_server import make_server

            make_server(args.state.absolute(), args.identity).run(transport="stdio")
            return
        result = asyncio.run(run(args))
        if result is not None:
            if args.command == "sessions" and args.table:
                print(sessions_table(result))
            else:
                print(json.dumps(result, indent=2))
    except (WireError, OSError, ValueError) as exc:
        if args.command == "hook":
            # Failed enrollment never blocks the session or changes permissions.
            print(
                json.dumps(
                    {"systemMessage": "Agent Wire reporting unavailable; chat can continue."}
                )
            )
        else:
            print(
                json.dumps({"error": {"code": getattr(exc, "code", "error"), "message": str(exc)}}),
                file=sys.stderr,
            )
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
