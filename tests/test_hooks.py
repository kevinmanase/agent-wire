# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_wire.broker import Broker
from agent_wire.hooks import run_hook
from agent_wire.paths import write_identity
from agent_wire.store import Store

from .test_store import enroll


@pytest.fixture
async def environment():
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        store = Store(state / "db")
        server = await asyncio.start_unix_server(Broker(store).handle, path=state / "broker.sock")
        try:
            async with server:
                yield state, store
        finally:
            store.close()


@pytest.mark.parametrize("runtime", ["codex", "claude"])
async def test_native_identity_is_reused_and_prompt_and_stop_preserve_reports(
    environment, monkeypatch, runtime
):
    state, store = environment
    a, other = enroll(store, "a", runtime), enroll(store, "other", runtime)
    write_identity(state, a)
    write_identity(state, other)
    monkeypatch.setenv("CODEX_THREAD_ID", other["agent"]["native_id"])
    monkeypatch.setenv("HERDR_PANE_ID", "unrelated-pane")

    async def discover(self, codex_socket):
        return {"sessions": [{**a["agent"], "endpoint": {"path": "/resumed/socket"}}]}

    async def validate(self, actual_runtime, native_id, endpoint):
        assert (actual_runtime, native_id) == (runtime, a["agent"]["native_id"])
        return endpoint

    monkeypatch.setattr("agent_wire.hooks.NativeAdapters.discover", discover)
    monkeypatch.setattr("agent_wire.adapters.NativeAdapters.validate", validate)
    store.session_update(a["session_handle"], task="Waiting for review", status="waiting")
    base = {"session_id": a["agent"]["native_id"]}
    for source in ("resume", "compact"):
        result = await run_hook(state, runtime, {**base, "source": source})
        assert a["session_handle"] in result["hookSpecificOutput"]["additionalContext"]
        assert len(store.agents()) == 2
        assert "/resumed/socket" in store.agent(a["agent"]["id"])["endpoint"]
    prompt = await run_hook(
        state,
        runtime,
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "PRIVATE RAW PROMPT"},
    )
    assert prompt["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    report = store.session(a["agent"]["id"])["report"]
    assert report["needs_update"]
    assert "PRIVATE RAW PROMPT" not in str(store.sessions())
    assert await run_hook(state, runtime, {**base, "hook_event_name": "Stop"}) == {}
    stopped = store.session(a["agent"]["id"])
    assert stopped["report"]["status"] == "waiting"
    assert stopped["activity"] == "idle"
    assert store.session(other["agent"]["id"])["freshness"] == "unseen"


async def test_claude_child_hook_does_not_write_parent_report(environment):
    state, store = environment
    a = enroll(store, runtime="claude")
    write_identity(state, a)
    result = await run_hook(
        state,
        "claude",
        {
            "session_id": a["agent"]["native_id"],
            "agent_id": "child-agent",
            "hook_event_name": "UserPromptSubmit",
        },
    )
    assert result == {}
    assert store.session(a["agent"]["id"])["last_seen"] is None


@pytest.mark.parametrize(
    "runtime,tool",
    [
        ("codex", "functions.request_user_input_async"),
        ("claude", "AskUserQuestion"),
    ],
)
async def test_questions_record_activity_without_claiming_task_status(environment, runtime, tool):
    state, store = environment
    a = enroll(store, runtime=runtime)
    write_identity(state, a)
    base = {"session_id": a["agent"]["native_id"]}
    result = await run_hook(
        state, runtime, {**base, "hook_event_name": "PreToolUse", "tool_name": tool}
    )
    assert result == {}
    session = store.session(a["agent"]["id"])
    assert session["activity"] == "waiting" and session["report"] is None
    await run_hook(state, runtime, {**base, "hook_event_name": "PostToolUse"})
    assert store.session(a["agent"]["id"])["activity"] == "working"


async def test_first_prompt_can_enroll_after_startup_discovery_was_unavailable(
    environment, monkeypatch
):
    state, store = environment
    target = {
        "runtime": "claude",
        "native_id": "new-native",
        "endpoint": {"path": "/fake/socket"},
        "cwd": "/repo",
    }

    async def discover(self, codex_socket):
        return {"sessions": [target]}

    async def validate(self, runtime, native_id, endpoint):
        assert (runtime, native_id) == ("claude", "new-native")
        return endpoint

    monkeypatch.setattr("agent_wire.hooks.NativeAdapters.discover", discover)
    monkeypatch.setattr("agent_wire.adapters.NativeAdapters.validate", validate)
    result = await run_hook(
        state, "claude", {"session_id": "new-native", "hook_event_name": "UserPromptSubmit"}
    )
    assert "session_update" in result["hookSpecificOutput"]["additionalContext"]
    session = store.sessions()["sessions"][0]
    assert session["native_id"] == "new-native" and session["activity"] == "working"
    assert len(list((state / "identities").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "permission_mode,expected",
    [("bypassPermissions", "bypass"), ("acceptEdits", "prompting"), ("plan", None), (None, None)],
)
async def test_hook_records_the_sessions_permission_class(environment, permission_mode, expected):
    state, store = environment
    a = enroll(store, runtime="claude")
    write_identity(state, a)
    payload = {"session_id": a["agent"]["native_id"], "hook_event_name": "PreToolUse"}
    if permission_mode:
        payload["permission_mode"] = permission_mode
    assert await run_hook(state, "claude", payload) == {}
    assert store.agent(a["agent"]["id"])["mode"] == expected
