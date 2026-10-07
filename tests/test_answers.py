# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import itertools
import json
import signal
import sys
from datetime import UTC, datetime

import pytest

from agent_wire import cli as cli_module
from agent_wire import hooks as hooks_module
from agent_wire.client import call
from agent_wire.errors import WireError
from agent_wire.hooks import answer_hook
from agent_wire.paths import write_identity
from agent_wire.store import Store

from .test_cli import cli
from .test_store import enroll

ASK = {"to": "user", "text": "Which color?", "kind": "decide", "native": True}
QUESTION = {
    "question": "Which color?",
    "header": "Color",
    "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}],
    "multiSelect": False,
}


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "db")
    yield db
    db.close()


def asking(store, ask=ASK):
    a = enroll(store, runtime="claude")
    store.session_update(a["session_handle"], task="Pick a color", status="waiting")
    session = store.session_ask(a["session_handle"], ask=ask)
    return a, session["report"]["ask"]["raised_at"]


def test_an_answer_lands_only_on_the_ask_raised_at_that_time(store):
    times = itertools.count(1000.0)
    store.clock = lambda: next(times)
    a, raised = asking(store)
    assert store.sessions()["sessions"][0]["report"]["ask"]["native"] is True
    token = a["session_handle"]
    with pytest.raises(WireError) as stale:
        store.answer("a", raised - 1, "Red")
    assert stale.value.code == "ask_closed"
    assert store.ask_poll(token, raised_at=raised) == {"open": True, "answer": None}
    assert store.answer("a", raised, "Red")["answered"]
    with pytest.raises(WireError) as again:
        store.answer(a["agent"]["id"], raised, "Blue")
    assert again.value.code == "already_answered"
    # Taking the answer clears the ask, so it is taken once.
    assert store.ask_poll(token, raised_at=raised) == {"open": False, "answer": "Red"}
    assert store.session(a["agent"]["id"])["report"]["ask"] is None
    assert store.ask_poll(token, raised_at=raised) == {"open": False, "answer": None}
    with pytest.raises(WireError) as closed:
        store.answer("a", raised, "Red")
    assert closed.value.code == "ask_closed"


def test_a_new_ask_drops_an_untaken_answer(store):
    times = itertools.count(1000.0)
    store.clock = lambda: next(times)
    a, raised = asking(store)
    store.answer("a", raised, "Red")
    newer = store.session_ask(a["session_handle"], ask={**ASK, "text": "Which shade?"})
    assert store.ask_poll(a["session_handle"], raised_at=raised)["open"] is False
    later = newer["report"]["ask"]["raised_at"]
    # A clear guarded by the old ask's time leaves the newer ask alone.
    store.session_ask(a["session_handle"], ask=None, if_raised_at=raised)
    assert store.ask_poll(a["session_handle"], raised_at=later) == {"open": True, "answer": None}


def test_only_a_native_ask_takes_an_answer(store):
    a, raised = asking(store, {key: ASK[key] for key in ("to", "text", "kind")})
    with pytest.raises(WireError) as refused:
        store.answer("a", raised, "Red")
    assert refused.value.code == "not_native"
    for bad in ({"session": "a", "raised_at": raised, "answer": " "}, {"raised_at": "1"}):
        with pytest.raises(WireError):
            store.answer(**{"session": "a", "raised_at": raised, "answer": "Red", **bad})


async def test_a_session_credential_cannot_answer(environment):
    state, store = environment
    a, raised = asking(store)
    peer = enroll(store, "peer")
    for token in (peer["session_handle"], a["session_handle"], None):
        with pytest.raises(WireError) as refused:
            await call(
                state,
                "ask_answer",
                session="a",
                raised_at=raised,
                answer="Red",
                session_handle=token,
            )
        assert refused.value.code == "forbidden"
    assert store.ask_poll(a["session_handle"], raised_at=raised)["answer"] is None
    # The local user's call, with no credential, is the one way in.
    assert (await call(state, "ask_answer", session="a", raised_at=raised, answer="Red"))[
        "answered"
    ]


def line(role, block, stamp=None):
    stamp = stamp or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": role, "timestamp": stamp, "message": {"content": [block]}}) + "\n"


def use(call_id, tool="AskUserQuestion"):
    return line("assistant", {"type": "tool_use", "id": call_id, "name": tool, "input": {}})


def result(call_id, stamp=None):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "answered"}
    return line("user", block, stamp)


def payload(a, tool="AskUserQuestion", tool_input=None, transcript=None):
    """Hook input for a dialog. Its transcript holds an older dialog of the tool, answered."""
    tool_input = {"questions": [QUESTION]} if tool_input is None else tool_input
    if transcript is None:
        transcript = a["transcript"]
        transcript.write_text(use("old", tool) + result("old", "2026-01-01T00:00:00.000Z"))
    return {
        "session_id": a["agent"]["native_id"],
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": tool_input,
        "transcript_path": str(transcript),
    }


async def raised_ask(store, a):
    for _ in range(200):
        if ask := store.session(a["agent"]["id"])["report"]["ask"]:
            return ask
        await asyncio.sleep(0.01)
    raise AssertionError("the hook raised no ask")


def reporting(state, store):
    a = enroll(store, runtime="claude")
    write_identity(state, a)
    store.session_update(a["session_handle"], task="Pick a color", status="working")
    return {**a, "transcript": state / "transcript.jsonl"}


@pytest.mark.parametrize(
    "tool,tool_input,answer,decision",
    [
        (
            "AskUserQuestion",
            {"questions": [QUESTION]},
            "Purple please",
            {
                "behavior": "allow",
                "updatedInput": {
                    "questions": [QUESTION],
                    "answers": {"Which color?": "Purple please"},
                },
            },
        ),
        (
            "ExitPlanMode",
            {"plan": "Do it"},
            "approve",
            {"behavior": "allow", "updatedInput": {"plan": "Do it"}},
        ),
        (
            "ExitPlanMode",
            {"plan": "Do it"},
            "Keep planning",
            {
                "behavior": "deny",
                "message": "The user did not approve the plan. Their answer: Keep planning",
            },
        ),
    ],
)
async def test_hook_relays_a_persons_answer(environment, tool, tool_input, answer, decision):
    state, store = environment
    a = reporting(state, store)
    task = asyncio.create_task(answer_hook(state, payload(a, tool, tool_input), poll=0.01))
    ask = await raised_ask(store, a)
    assert ask["native"] and len(ask["options"]) == 2
    await cli(state, "answer", "a", "--ask-at", repr(ask["raised_at"]), "--", *answer.split())
    result = await asyncio.wait_for(task, 5)
    assert result == {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}
    }
    assert store.session(a["agent"]["id"])["report"]["ask"] is None


async def test_terminal_answer_wins_and_the_hook_exits_quietly(environment):
    state, store = environment
    a = reporting(state, store)
    task = asyncio.create_task(answer_hook(state, payload(a), poll=0.01))
    ask = await raised_ask(store, a)
    with a["transcript"].open("a") as stream:
        # Neither the older dialog's result nor another tool's ends the wait.
        stream.write(use("toolu_0", "Bash") + result("toolu_0"))
        stream.flush()
        await asyncio.sleep(0.1)
        assert not task.done()
        # Claude can write the dialog's call only with its result.
        stream.write(use("toolu_1") + result("toolu_1"))
    assert await asyncio.wait_for(task, 5) == {}
    assert store.session(a["agent"]["id"])["report"]["ask"] is None
    with pytest.raises(WireError):
        store.answer("a", ask["raised_at"], "Red")


async def test_without_an_answer_the_hook_decides_nothing(environment):
    state, store = environment
    a = reporting(state, store)
    # Waiting runs out: no decision, and the hook clears its own ask.
    assert await answer_hook(state, payload(a), wait=0, poll=0.01) == {}
    assert store.session(a["agent"]["id"])["report"]["ask"] is None
    # A replaced ask ends the wait too, leaving the newer ask alone.
    task = asyncio.create_task(answer_hook(state, payload(a), poll=0.01))
    await raised_ask(store, a)
    newer = store.session_ask(a["session_handle"], ask={**ASK, "text": "Something else?"})
    assert await asyncio.wait_for(task, 5) == {}
    assert store.session(a["agent"]["id"])["report"]["ask"] == newer["report"]["ask"]


async def test_a_multi_question_dialog_is_shown_but_answered_in_the_terminal(environment):
    state, store = environment
    a = reporting(state, store)
    second = {**QUESTION, "question": "Which size?"}
    task = asyncio.create_task(
        answer_hook(state, payload(a, tool_input={"questions": [QUESTION, second]}), poll=0.01)
    )
    ask = await raised_ask(store, a)
    assert ask["text"] == "Which color? / Which size?"
    assert "native" not in ask and "options" not in ask
    with pytest.raises(WireError) as refused:
        await call(state, "ask_answer", session="a", raised_at=ask["raised_at"], answer="Red")
    assert refused.value.code == "not_native"
    store.session_ask(a["session_handle"], ask=None)
    assert await asyncio.wait_for(task, 5) == {}


async def test_hook_skips_what_it_cannot_show(environment):
    state, store = environment
    a = enroll(store, runtime="claude")
    write_identity(state, a)
    a["transcript"] = state / "transcript.jsonl"
    # No report to carry the ask, another event, or a subagent's dialog.
    assert await answer_hook(state, payload(a), poll=0.01) == {}
    for extra in ({"hook_event_name": "PreToolUse"}, {"agent_id": "child"}, {"tool_name": "Bash"}):
        assert await answer_hook(state, {**payload(a), **extra}, poll=0.01) == {}
    assert store.session(a["agent"]["id"])["report"] is None


async def test_answer_hook_without_a_broker_leaves_the_dialog(tmp_path):
    hook_input = json.dumps(
        {
            "session_id": "s",
            "hook_event_name": "PermissionRequest",
            "tool_name": "ExitPlanMode",
            "tool_input": {},
        }
    )
    write_identity(
        tmp_path,
        {"agent": {"id": "x", "runtime": "claude", "native_id": "s"}, "session_handle": "t"},
    )
    result = await cli(tmp_path, "hook", "claude", "--answer", stdin=hook_input.encode())
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result


async def test_answer_command_sends_no_credential(monkeypatch, tmp_path):
    requests = []

    async def fake_call(state, method, **params):
        requests.append((method, params))

    monkeypatch.setattr(cli_module, "call", fake_call)
    args = cli_module.parser().parse_args(
        ["--state", str(tmp_path), "answer", "lead", "--ask-at", "1791.25", "--", "-x", "two words"]
    )
    await cli_module.run(args)
    assert requests == [
        ("ask_answer", {"session": "lead", "raised_at": 1791.25, "answer": "-x two words"})
    ]


def test_a_new_dialog_never_inherits_an_untaken_answer(store):
    times = itertools.count(1000.0)
    store.clock = lambda: next(times)
    a, raised = asking(store)
    store.answer("a", raised, "Approve")
    # The same dialog again, as every plan approval is: a new ask, without the old answer.
    again = store.session_ask(a["session_handle"], ask=ASK)["report"]["ask"]["raised_at"]
    assert again != raised
    assert store.ask_poll(a["session_handle"], raised_at=again) == {"open": True, "answer": None}
    with pytest.raises(WireError):
        store.answer("a", raised, "Approve")


async def test_a_killed_hook_clears_its_ask_and_decides_nothing(environment):
    state, store = environment
    a = reporting(state, store)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_wire",
        "--state",
        str(state),
        "hook",
        "claude",
        "--answer",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    process.stdin.write(json.dumps(payload(a)).encode())
    process.stdin.close()
    await raised_ask(store, a)
    process.send_signal(signal.SIGTERM)
    stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    assert "decision" not in stdout.decode()
    assert store.session(a["agent"]["id"])["report"]["ask"] is None


def test_raised_at_always_moves_forward_even_on_a_stuck_clock(store):
    store.clock = lambda: 1000.0
    a, first = asking(store)
    store.answer("a", first, "Approve")
    store.session_ask(a["session_handle"], ask=None)
    # Plan A's answer waits on its own raised_at; plan B, raised at the same clock time, can't
    # take it, nor can a full report that raises the dialog again.
    second = store.session_ask(a["session_handle"], ask=ASK)["report"]["ask"]["raised_at"]
    third = store.session_update(a["session_handle"], task="T", status="waiting", ask=ASK)
    third = third["report"]["ask"]["raised_at"]
    assert first < second < third
    assert store.ask_poll(a["session_handle"], raised_at=third) == {"open": True, "answer": None}
    with pytest.raises(WireError):
        store.answer("a", first, "Approve")


def fake_poll(monkeypatch, *replies, delay=0, during=None):
    """Answer ask_poll with each reply in turn, after `delay`, running `during` meanwhile."""
    replies = iter(replies)

    async def fake_call(state, method, **params):
        if method != "ask_poll":
            return await call(state, method, **params)
        if during:
            during()
        await asyncio.sleep(delay)
        return next(replies, {"open": False, "answer": None})

    monkeypatch.setattr(hooks_module, "call", fake_call)


@pytest.mark.parametrize(
    "bad",
    [False, {}, "", "  ", ["Red"], "x" * 4097],
    ids=["false", "object", "empty", "blank", "list", "too-long"],
)
async def test_hook_relays_only_a_real_answer(environment, monkeypatch, bad):
    state, store = environment
    a = reporting(state, store)
    fake_poll(monkeypatch, {"open": True, "answer": bad})
    assert await asyncio.wait_for(answer_hook(state, payload(a), poll=0.01), 5) == {}


async def test_an_answer_after_the_terminal_won_is_not_relayed(environment, monkeypatch):
    state, store = environment
    a = reporting(state, store)

    def terminal_answers():
        with a["transcript"].open("a") as stream:
            stream.write(use("toolu_1") + result("toolu_1"))

    fake_poll(monkeypatch, {"open": False, "answer": "Red"}, delay=0.05, during=terminal_answers)
    assert await asyncio.wait_for(answer_hook(state, payload(a), poll=0.01), 5) == {}
    # Past the deadline when the poll returns: no decision either.
    fake_poll(monkeypatch, {"open": False, "answer": "Red"}, delay=0.05)
    assert await asyncio.wait_for(answer_hook(state, payload(a), wait=0.01, poll=0.01), 5) == {}


async def test_hook_stops_when_the_transcript_stays_unreadable(environment, monkeypatch):
    state, store = environment
    a = reporting(state, store)
    monkeypatch.setattr(hooks_module, "TRANSCRIPT_GRACE", 0.05)
    hook_input = payload(a, transcript=state / "missing.jsonl")
    assert await asyncio.wait_for(answer_hook(state, hook_input, poll=0.01), 5) == {}
    assert store.session(a["agent"]["id"])["report"]["ask"] is None
