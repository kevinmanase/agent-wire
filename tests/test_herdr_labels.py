# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/herdr-labels/scripts/herdr_tab.py"
SPEC = importlib.util.spec_from_file_location("herdr_tab", SCRIPT)
labels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(labels)


@pytest.fixture
def herdr(monkeypatch):
    state = SimpleNamespace(tab="tab-a", labels={"tab-a": "⚪ ready"}, calls=[], failure=None)
    state.env = {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "pane-a",
        "HERDR_TAB_ID": "stale-tab",
        "HERDR_BIN_PATH": "/fake/herdr",
    }

    def run(argv, **kwargs):
        assert argv[0] == "/fake/herdr"
        assert "shell" not in kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert 0 < kwargs["timeout"] <= labels.CALL_TIMEOUT
        command = argv[1:]
        state.calls.append(command)
        if state.failure:
            raise state.failure
        if command == ["pane", "get", "pane-a"]:
            output = {"result": {"pane": {"tab_id": state.tab}}}
        elif command[:2] == ["tab", "get"]:
            output = {"result": {"tab": {"label": state.labels[command[2]]}}}
        elif command[:2] == ["tab", "rename"]:
            state.labels[command[2]] = command[3]
            output = {"result": {}}
        elif command[:2] == ["notification", "show"]:
            output = {"result": {}}
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(output))

    monkeypatch.setattr(labels.subprocess, "run", run)
    return state


def invoke(herdr, *args):
    return labels.main(list(args), env=herdr.env)


def hook(herdr, event, runtime="codex", **payload):
    return labels.main(
        ["hook", runtime],
        env=herdr.env,
        stdin=io.StringIO(json.dumps({"hook_event_name": event, **payload})),
    )


def test_name_status_and_moved_pane(herdr):
    assert invoke(herdr, "name", "🔨 build tests") == 0
    assert invoke(herdr, "status", "working") == 0
    assert herdr.labels["tab-a"] == "⏳ 🔨 build tests"
    herdr.tab = "tab-b"
    herdr.labels["tab-b"] = "⏳ 👀 review"
    assert invoke(herdr, "status", "done") == 0
    assert herdr.labels == {"tab-a": "⏳ 🔨 build tests", "tab-b": "✅ 👀 review"}
    assert "stale-tab" not in str(herdr.calls)
    assert herdr.calls[-3:] == [
        ["pane", "get", "pane-a"],
        ["tab", "get", "tab-b"],
        ["tab", "rename", "tab-b", "✅ 👀 review"],
    ]


@pytest.mark.parametrize("attention,marker", [("ask", "❓"), ("request", "❗")])
def test_pending_attention_survives_status_and_hooks_until_explicit_resolve(
    herdr, attention, marker
):
    invoke(herdr, "name", "🚀 release")
    invoke(herdr, attention, "choose the release target")
    for status in ("working", "done", "ready"):
        invoke(herdr, "status", status)
    for event, payload in (
        ("UserPromptSubmit", {}),
        ("Stop", {}),
        ("PreToolUse", {"tool_name": "functions.request_user_input_async"}),
        ("PermissionRequest", {}),
        ("PostToolUse", {"tool_name": "functions.request_user_input_async"}),
    ):
        hook(herdr, event, **payload)
        assert herdr.labels["tab-a"] == f"{marker} choose the release target · 🚀 release"
    invoke(herdr, "resolve")
    assert herdr.labels["tab-a"] == "⏳ 🚀 release"
    hook(herdr, "Stop")
    assert herdr.labels["tab-a"] == "✅ 🚀 release"


def test_name_preserves_attention_and_explicit_ask_can_replace_it(herdr):
    invoke(herdr, "request", "review deployment")
    invoke(herdr, "name", "review staging deployment")
    assert herdr.labels["tab-a"] == "❗ review deployment · review staging deployment"
    invoke(herdr, "ask", "which region?")
    assert herdr.labels["tab-a"] == "❓ which region? · review staging deployment"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("source", ["startup", "clear"])
@pytest.mark.parametrize("crown", [True, False])
def test_new_session_resets_label_but_keeps_orchestrator_name(herdr, runtime, source, crown):
    invoke(herdr, "name", "👑 orchestrator" if crown else "old task")
    invoke(herdr, "ask", "old question")
    assert hook(herdr, "SessionStart", runtime, source=source) == 0
    assert herdr.labels["tab-a"] == ("⚪ 👑 orchestrator" if crown else "⚪ ready")


@pytest.mark.parametrize("source", ["resume", "compact", "fork"])
def test_resume_and_compact_preserve_labels_without_calls(herdr, source):
    hook(herdr, "SessionStart", source=source)
    assert herdr.calls == []


@pytest.mark.parametrize(
    "runtime,tool",
    [
        ("claude", "AskUserQuestion"),
        ("claude", "functions.AskUserQuestion"),
        ("codex", "request_user_input"),
        ("codex", "functions.request_user_input_async"),
    ],
)
def test_question_hooks_flag_attention_without_copying_payload(herdr, runtime, tool):
    herdr.labels["tab-a"] = "⏳ 👀 review"
    hook(herdr, "PreToolUse", runtime, tool_name=tool, tool_input={"question": "PRIVATE QUESTION"})
    assert herdr.labels["tab-a"] == "❓ answer the question · 👀 review"
    assert "PRIVATE" not in str(herdr.calls)
    calls = list(herdr.calls)
    hook(herdr, "PostToolUse", runtime, tool_name=tool)
    assert herdr.calls == calls


def test_lifecycle_and_permission_attention(herdr, capsys):
    hook(herdr, "UserPromptSubmit", prompt="PRIVATE PROMPT")
    assert herdr.labels["tab-a"] == "⏳ ready"
    hook(herdr, "Stop")
    assert herdr.labels["tab-a"] == "✅ ready"
    hook(herdr, "PermissionRequest", tool_input={"command": "SECRET"})
    assert herdr.labels["tab-a"] == "❗ review permission in codex · ready"
    assert "PRIVATE" not in str(herdr.calls) and "SECRET" not in str(herdr.calls)
    assert capsys.readouterr() == ("{}\n" * 3, "")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "null",
        "1",
        "{}",
        '{"hook_event_name": "Unknown"}',
        '{"hook_event_name": "PreToolUse", "tool_name": []}',
        '{"hook_event_name": "SessionStart", "source": []}',
        '{"hook_event_name": "PreToolUse", "tool_name": "exec_command"}',
        '{"hook_event_name": "Stop", "agent_id": "child"}',
        '{"hook_event_name": "Stop", "agent_id": null}',
        "x" * (labels.MAX_INPUT + 1),
    ],
)
def test_invalid_unrelated_and_child_hooks_do_nothing(herdr, payload, capsys):
    assert labels.main(["hook", "claude"], env=herdr.env, stdin=io.StringIO(payload)) == 0
    assert herdr.calls == []
    assert capsys.readouterr() == ("{}\n", "")


@pytest.mark.parametrize("env", [{}, {"HERDR_ENV": "1"}, {"HERDR_PANE_ID": "p"}])
def test_outside_herdr_is_noop_for_hooks_and_explicit_error_for_cli(herdr, env, capsys):
    assert labels.main(["hook", "codex"], env=env, stdin=io.StringIO("{}")) == 0
    assert capsys.readouterr() == ("{}\n", "")
    assert labels.main(["status", "done"], env=env) == 1
    assert "HERDR_ENV=1 and HERDR_PANE_ID" in capsys.readouterr().err
    assert herdr.calls == []


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("herdr", 1)])
def test_failure_is_silent_for_hooks_and_reported_for_cli(herdr, failure, capsys):
    herdr.failure = failure
    assert hook(herdr, "Stop") == 0
    assert capsys.readouterr() == ("{}\n", "")
    assert invoke(herdr, "status", "done") == 1
    assert "unavailable or timed out" in capsys.readouterr().err


@pytest.mark.parametrize("stdout,returncode", [("not json", 0), ("{}", 0), ("{}", 1)])
def test_failed_or_malformed_herdr_responses_fail_open(
    herdr, monkeypatch, stdout, returncode, capsys
):
    monkeypatch.setattr(
        labels.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=stdout, returncode=returncode),
    )
    assert hook(herdr, "Stop") == 0
    assert capsys.readouterr() == ("{}\n", "")


def test_names_strip_controls_and_are_bounded(herdr):
    invoke(herdr, "name", "\x1b[31mred\x1b[0m\nnew\tline\x00\u202e ")
    assert herdr.labels["tab-a"] == "⚪ red new line"
    invoke(herdr, "ask", "x" * 200)
    assert herdr.labels["tab-a"] == "❓ " + "x" * labels.MAX_NAME + " · red new line"
    assert invoke(herdr, "name", "\x1b[31m\n") == 1


def test_notifications_are_opt_in_best_effort_and_not_repeated(herdr, monkeypatch):
    invoke(herdr, "ask", "choose a target")
    assert not any(call[0] == "notification" for call in herdr.calls)
    invoke(herdr, "resolve")
    herdr.env["HERDR_LABEL_NOTIFICATIONS"] = "1"
    hook(herdr, "PermissionRequest")
    assert herdr.calls[-1] == [
        "notification",
        "show",
        "Agent needs you",
        "--body",
        "review permission in codex",
        "--sound",
        "request",
    ]
    count = len(herdr.calls)
    hook(herdr, "PermissionRequest")
    assert len(herdr.calls) == count + 2
    invoke(herdr, "resolve")
    original_run = labels.subprocess.run

    def fail_notification(argv, **kwargs):
        if argv[1] == "notification":
            raise OSError("notification unavailable")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(labels.subprocess, "run", fail_notification)
    assert invoke(herdr, "request", "check the deployment") == 0
    assert herdr.labels["tab-a"] == "❗ check the deployment · ready"


def test_async_question_survives_name_updates_and_preserves_orchestrator(herdr):
    invoke(herdr, "name", "👑 orchestrator")
    hook(herdr, "PreToolUse", tool_name="functions.request_user_input_async")
    invoke(herdr, "ask", "ship today?")
    invoke(herdr, "name", "👑 orchestrator release")
    hook(herdr, "PostToolUse", tool_name="functions.request_user_input_async")
    hook(herdr, "Stop")
    assert herdr.labels["tab-a"] == "❓ ship today? · 👑 orchestrator release"
    invoke(herdr, "resolve")
    assert herdr.labels["tab-a"] == "⏳ 👑 orchestrator release"


def test_middle_dot_input_cannot_change_ask_task_boundary(herdr):
    invoke(herdr, "name", "🧪 login · tests")
    invoke(herdr, "ask", "ship · today?")
    assert herdr.labels["tab-a"] == "❓ ship / today? · 🧪 login / tests"
    invoke(herdr, "resolve")
    assert herdr.labels["tab-a"] == "⏳ 🧪 login / tests"


def test_total_time_budget_stops_further_calls(herdr, monkeypatch):
    ticks = iter([0.0, 0.1, labels.TOTAL_TIMEOUT + 0.1])
    monkeypatch.setattr(labels.time, "monotonic", lambda: next(ticks))
    assert hook(herdr, "Stop") == 0
    assert herdr.calls == [["pane", "get", "pane-a"]]
