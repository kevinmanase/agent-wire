#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional, standard-library-only Herdr tab labels for Claude Code and Codex."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

MARKERS = {"working": "⏳", "done": "✅", "ready": "⚪", "ask": "❓", "request": "❗"}
ATTENTION = {"ask", "request"}
MAX_NAME = 96
MAX_INPUT = 64 * 1024
CALL_TIMEOUT = 1.0
TOTAL_TIMEOUT = 3.0
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


class HerdrError(Exception):
    """A bounded Herdr command failed or returned an invalid response."""


def clean_text(value):
    """Strip terminal controls, including ANSI sequences."""
    value = ANSI.sub("", value)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    return " ".join(value.split())


def clean_name(value):
    """Bound each label field and reserve the middle dot for the ask separator."""
    return clean_text(value).replace("·", "/")[:MAX_NAME].strip()


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise HerdrError("Herdr returned an invalid identifier")
    value = str(value)
    if (
        not value
        or len(value) > 256
        or value.startswith("-")
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    ):
        raise HerdrError("Herdr returned an invalid identifier")
    return value


def split_label(label):
    label = clean_text(label)
    for status, marker in MARKERS.items():
        if label.startswith(marker):
            rest = label[len(marker) :].strip()
            if status in ATTENTION:
                ask, separator, name = rest.partition(" · ")
                if separator:
                    return status, clean_name(ask), clean_name(name) or "ready"
                # Older status-only labels retain their task until an explicit ask.
                return (
                    status,
                    "answer the question" if status == "ask" else "action needed",
                    (clean_name(rest) or "ready"),
                )
            return status, None, clean_name(rest) or "ready"
    return "ready", None, clean_name(label) or "ready"


class Herdr:
    def __init__(self, env):
        self.binary = env.get("HERDR_BIN_PATH") or "herdr"
        self.pane = identifier(env["HERDR_PANE_ID"])
        self.notifications = env.get("HERDR_LABEL_NOTIFICATIONS") == "1"
        self.deadline = time.monotonic() + TOTAL_TIMEOUT

    def call(self, *args, json_result=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise HerdrError("Herdr label update timed out")
        try:
            result = subprocess.run(
                [self.binary, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=min(CALL_TIMEOUT, remaining),
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
            raise HerdrError("Herdr is unavailable or timed out") from error
        if result.returncode:
            raise HerdrError("Herdr command failed")
        if not json_result:
            return None
        try:
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or response.get("ok") is False:
                raise ValueError("Invalid response")
            value = response["result"]
            if not isinstance(value, dict):
                raise ValueError("Invalid result")
            return value
        except (KeyError, ValueError, TypeError) as error:
            raise HerdrError("Herdr returned an invalid response") from error

    def current(self):
        # Pane location is authoritative. HERDR_TAB_ID may be stale after a move.
        try:
            tab = identifier(
                self.call("pane", "get", self.pane, json_result=True)["pane"]["tab_id"]
            )
            label = self.call("tab", "get", tab, json_result=True)["tab"]["label"]
            if not isinstance(label, str):
                raise ValueError("Invalid label")
            return tab, label
        except (KeyError, TypeError, ValueError) as error:
            raise HerdrError("Herdr returned an invalid pane or tab") from error

    def update(self, command, text=None, *, hook=False):
        tab, label = self.current()
        status, ask, name = split_label(label)
        previous_status = status
        if command == "name":
            name = text
        elif command == "startup":
            status, name = "ready", name if "👑" in name else "ready"
            ask = None
        elif command == "resolve":
            status = "working"
            ask = None
        elif status in ATTENTION and (hook or command not in ATTENTION):
            # Only an explicit resolve, new session, or explicit new ask clears it.
            pass
        else:
            status = command
            if status in ATTENTION:
                ask = text
        content = f"{ask} · {name}" if status in ATTENTION else name
        target = f"{MARKERS[status]} {content}"
        if target == label:
            return
        self.call("tab", "rename", tab, target, json_result=True)
        if self.notifications and status in ATTENTION and previous_status not in ATTENTION:
            try:
                self.call(
                    "notification",
                    "show",
                    "Agent needs you",
                    "--body",
                    ask,
                    "--sound",
                    "request",
                    json_result=True,
                )
            except HerdrError:
                pass


def hook_command(payload):
    """Interpret lifecycle metadata only. Never read prompts or tool arguments."""
    if not isinstance(payload, dict) or "agent_id" in payload:
        return None
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        source = payload.get("source")
        return "startup" if isinstance(source, str) and source in {"startup", "clear"} else None
    if event == "UserPromptSubmit":
        return "working"
    if event == "Stop":
        return "done"
    if event == "PermissionRequest":
        return "request"
    if event == "PreToolUse":
        tool = payload.get("tool_name")
        if isinstance(tool, str) and tool.rsplit(".", 1)[-1] in {
            "AskUserQuestion",
            "request_user_input",
            "request_user_input_async",
        }:
            return "ask"
    return None


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("name", "ask", "request"):
        commands.add_parser(command).add_argument("text")
    commands.add_parser("status").add_argument("status", choices=("working", "done", "ready"))
    commands.add_parser("resolve")
    commands.add_parser("hook").add_argument("runtime", choices=("claude", "codex"))
    return result


def execute(args, env, stdin):
    is_hook = args.command == "hook"
    if env.get("HERDR_ENV") != "1" or not env.get("HERDR_PANE_ID"):
        if not is_hook:
            print("Herdr labels require HERDR_ENV=1 and HERDR_PANE_ID", file=sys.stderr)
        return 0 if is_hook else 1
    try:
        command = args.command
        text = None
        if is_hook:
            raw = stdin.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                return 0
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                return 0
            command = hook_command(payload)
            if command is None:
                return 0
            if command == "ask":
                text = "answer the question"
            elif command == "request":
                text = f"review permission in {args.runtime}"
        elif command == "status":
            command = args.status
        elif command in {"name", "ask", "request"}:
            text = clean_name(args.text)
            if not text:
                raise HerdrError("A label needs visible text")
        Herdr(env).update(command, text, hook=is_hook)
        return 0
    except (HerdrError, OSError, RecursionError) as error:
        if not is_hook:
            print(f"herdr labels: {error}", file=sys.stderr)
        return 0 if is_hook else 1


def main(argv=None, *, env=None, stdin=None):
    args = parser().parse_args(argv)
    try:
        return execute(
            args,
            os.environ if env is None else env,
            sys.stdin if stdin is None else stdin,
        )
    finally:
        if args.command == "hook":
            # Valid for both runtimes, including Codex Stop. No permission decision.
            print("{}")


if __name__ == "__main__":
    raise SystemExit(main())
