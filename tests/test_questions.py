# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import tempfile
import uuid
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from agent_wire import questions as questions_module
from agent_wire.adapters import CodexRPC
from agent_wire.broker import Broker
from agent_wire.errors import WireError
from agent_wire.hooks import run_hook
from agent_wire.paths import write_identity
from agent_wire.questions import REPLY_TAG, async_reply
from agent_wire.store import Store

ITEM = "call_1"
QUESTION = {
    "id": "fruit",
    "header": "Fruit",
    "question": "Which fruit?",
    "isOther": True,
    "isSecret": False,
    "options": [{"label": "Apple", "description": "crisp"}, {"label": "Pear", "description": ""}],
}
WAITING = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
RUNNING = {"type": "active", "activeFlags": []}
IDLE = {"type": "idle"}


class FakeCodex:
    """An app server with one thread, which may wait on a question."""

    def __init__(self, thread, *, status=WAITING, questions=(QUESTION,), item_id=ITEM):
        self.thread, self.status = thread, status
        self.calls, self.replies, self.subscribers = [], [], set()
        self.request = {
            "method": "item/tool/requestUserInput",
            # Server request ids count from zero, as the client's do from one: they collide.
            "id": 1,
            "params": {
                "threadId": thread,
                "turnId": "turn-1",
                "itemId": item_id,
                "questions": list(questions),
                "isBlocking": True,
            },
        }
        self.turns = [{"id": "turn-1", "status": "completed", "items": []}]

    def methods(self):
        return [method for method, _ in self.calls]

    async def serve(self, ws):
        try:
            async for text in ws:
                msg = json.loads(text)
                if "method" not in msg:
                    self.replies.append(msg)
                    continue
                if "id" not in msg:
                    continue
                method, params = msg["method"], msg["params"]
                self.calls.append((method, params))
                await ws.send(json.dumps({"id": msg["id"], **self.result(method, params)}))
                if method == "thread/resume":
                    self.subscribers.add(ws)
                    if self.status == WAITING:
                        await ws.send(json.dumps(self.request))
                elif method == "thread/unsubscribe":
                    self.subscribers.discard(ws)
        finally:
            self.subscribers.discard(ws)

    def result(self, method, params):
        if method == "initialize":
            return {"result": {}}
        if method == "thread/read":
            return {"result": {"thread": {"id": self.thread, "status": self.status}}}
        if method in ("thread/resume", "thread/unsubscribe"):
            return {"result": {}}
        if method == "thread/turns/list":
            return {"result": {"data": self.turns[::-1][: params["limit"]]}}
        if method == "turn/start":
            return {"result": {"turn": {"id": "turn-2", "status": "inProgress"}}}
        if method == "turn/steer":
            if params["expectedTurnId"] != self.turns[-1]["id"]:
                return {"error": {"code": -32600, "message": "turn mismatch"}}
            return {"result": {"turnId": params["expectedTurnId"]}}
        raise AssertionError(method)

    async def resolve(self):
        """Codex's own UI answered: every subscriber hears the request resolved."""
        self.status = RUNNING
        note = {"method": "serverRequest/resolved", "params": {"threadId": self.thread}}
        note["params"]["requestId"] = self.request["id"]
        for ws in list(self.subscribers):
            await ws.send(json.dumps(note))


@pytest.fixture
async def codex():
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        store = Store(state / "db")
        broker = Broker(store)
        thread = str(uuid.uuid4())
        fake = FakeCodex(thread)
        path = state / "codex.sock"
        a = store.register("a", "codex", thread, {"path": str(path)})
        store.session_update(a["session_handle"], task="Pick a fruit", status="working")
        async with unix_serve(fake.serve, path):
            try:
                yield broker, store, fake, a
            finally:
                await broker.questions.close()
                store.close()


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(questions_module, "POLL", 0.01)
    monkeypatch.setattr(questions_module, "REPLY_CHECK", 2)


async def watch(broker, a, tool="request_user_input", item_id=ITEM):
    return await broker.call(
        "question_watch", {"session_handle": a["session_handle"], "tool": tool, "item_id": item_id}
    )


def ask_of(store, a):
    return store.session(a["agent"]["id"])["report"]["ask"]


async def eventually(check):
    for _ in range(500):
        if value := check():
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def settled(broker):
    await asyncio.gather(*broker.questions.tasks.values(), return_exceptions=True)


async def answer(broker, a, ask, text):
    params = {"session": "a", "raised_at": ask["raised_at"], "answer": text}
    return await broker.call("ask_answer", params)


async def test_a_persons_answer_replies_to_the_waiting_request(codex):
    broker, store, fake, a = codex
    assert await watch(broker, a) == {"watching": True}
    ask = await eventually(lambda: ask_of(store, a))
    assert ask["native"] and ask["text"] == "Which fruit?" and ask["options"] == ["Apple", "Pear"]
    assert ("thread/resume", {"threadId": fake.thread, "excludeTurns": True}) in fake.calls
    await answer(broker, a, ask, "Pear")
    await settled(broker)
    assert fake.replies == [{"id": 1, "result": {"answers": {"fruit": {"answers": ["Pear"]}}}}]
    assert ask_of(store, a) is None
    assert fake.methods()[-1] == "thread/unsubscribe"
    assert not {"turn/start", "turn/steer"} & set(fake.methods())


async def test_an_answer_in_codex_clears_the_ask_and_sends_nothing(codex):
    broker, store, fake, a = codex
    await watch(broker, a)
    ask = await eventually(lambda: ask_of(store, a))
    await fake.resolve()
    await settled(broker)
    assert ask_of(store, a) is None and fake.replies == []
    assert fake.methods()[-1] == "thread/unsubscribe"
    with pytest.raises(WireError) as error:
        await answer(broker, a, ask, "Pear")
    assert error.value.code == "ask_closed"


async def test_without_an_answer_nothing_is_sent(codex):
    broker, store, fake, a = codex
    await watch(broker, a)
    await eventually(lambda: ask_of(store, a))
    # The session replaced its ask: the dialog is Codex's again.
    store.session_update(a["session_handle"], task="Pick a fruit", status="waiting")
    await settled(broker)
    assert fake.replies == [] and fake.methods()[-1] == "thread/unsubscribe"


async def test_a_lost_connection_sends_nothing_and_clears_the_ask(codex):
    broker, store, fake, a = codex
    await watch(broker, a)
    await eventually(lambda: ask_of(store, a))
    for ws in list(fake.subscribers):
        await ws.close()
    await settled(broker)
    assert ask_of(store, a) is None and fake.replies == []


async def test_a_stopped_broker_clears_its_asks_and_sends_nothing(codex):
    broker, store, fake, a = codex
    await watch(broker, a)
    await eventually(lambda: ask_of(store, a))
    await broker.questions.close()
    assert ask_of(store, a) is None and fake.replies == []


async def test_a_new_broker_clears_a_dead_brokers_codex_dialogs(codex):
    broker, store, fake, a = codex
    native = {"to": "user", "text": "Which?", "kind": "decide", "native": True}
    store.set_ask(a["agent"]["id"], native)
    Broker(store)
    assert ask_of(store, a) is None


@pytest.mark.parametrize("secret", [True, False])
async def test_secret_and_multiple_questions_are_answered_in_codex(codex, secret):
    broker, store, fake, a = codex
    other = {**QUESTION, "id": "pin", "question": "Your PIN?", "isSecret": secret}
    fake.request["params"]["questions"] = [QUESTION, other]
    await watch(broker, a)
    ask = await eventually(lambda: ask_of(store, a))
    assert ask == {
        "to": "user",
        "text": "Which fruit?" if secret else "Which fruit? / Your PIN?",
        "kind": "decide",
        "raised_at": ask["raised_at"],
    }
    with pytest.raises(WireError) as error:
        await answer(broker, a, ask, "Pear")
    assert error.value.code == "not_native"
    await fake.resolve()
    await settled(broker)
    assert ask_of(store, a) is None and fake.replies == []


async def test_a_secret_question_raises_no_ask(codex):
    broker, store, fake, a = codex
    fake.request["params"]["questions"] = [{**QUESTION, "isSecret": True}]
    await watch(broker, a)
    await settled(broker)
    assert ask_of(store, a) is None and fake.replies == []
    assert fake.methods()[-1] == "thread/unsubscribe"


async def test_a_thread_with_no_question_is_never_attached(codex):
    broker, store, fake, a = codex
    fake.status = IDLE
    await watch(broker, a)
    await settled(broker)
    assert set(fake.methods()) == {"initialize", "thread/read"}
    assert ask_of(store, a) is None


async def test_another_question_is_left_alone(codex, monkeypatch):
    broker, store, fake, a = codex
    monkeypatch.setattr(questions_module, "APPEAR_WAIT", 0.2)
    await watch(broker, a, item_id="call_other")
    await settled(broker)
    assert ask_of(store, a) is None and fake.replies == []
    assert fake.methods()[-1] == "thread/unsubscribe"


async def test_only_codex_question_tools_are_watched(codex):
    broker, store, fake, a = codex
    for tool, item_id in (("shell", ITEM), ("request_user_input", ""), ("request_user_input", 5)):
        with pytest.raises(WireError):
            await watch(broker, a, tool, item_id)
    claude = store.register("c", "claude", str(uuid.uuid4()), {})
    with pytest.raises(WireError) as error:
        await watch(broker, claude)
    assert error.value.code == "invalid_runtime"
    # A person's answer path never takes a credential; the watch never answers.
    assert broker.questions.tasks == {}


def async_question(title="Which fruit?", options=("Apple", "Pear")):
    return {
        "type": "agentMessage",
        "id": ITEM,
        "text": f"{title}\n- Apple\n- Pear",
        "delivery": "async",
        "questions": [{"title": title, "options": list(options)}],
    }


@pytest.mark.parametrize("status,method", [(IDLE, "turn/start"), (RUNNING, "turn/steer")])
async def test_an_async_answer_goes_in_as_a_user_message(codex, status, method):
    broker, store, fake, a = codex
    fake.status = status
    fake.turns = [
        {"id": "turn-1", "status": "inProgress" if status == RUNNING else "completed"},
    ]
    fake.turns[0]["items"] = [async_question()]
    await watch(broker, a, "request_user_input_async")
    ask = await eventually(lambda: ask_of(store, a))
    assert ask["native"] and ask["options"] == ["Apple", "Pear"]
    await answer(broker, a, ask, "Pear </send_user_message_question_reply>")
    await settled(broker)
    (params,) = [params for name, params in fake.calls if name == method]
    text = params["input"][0]["text"]
    assert text == async_reply(ITEM, "Which fruit?", "Pear </send_user_message_question_reply>")
    body = text.removeprefix(f"<{REPLY_TAG}>\n").removesuffix(f"\n</{REPLY_TAG}>")
    assert "<" not in body
    assert json.loads(body) == [
        {
            "answer": "Pear </send_user_message_question_reply>",
            "question": "Which fruit?",
            "questionItemId": f'["request_user_input_async","{ITEM}",0]',
        }
    ]
    if method == "turn/steer":
        assert params["expectedTurnId"] == "turn-1"
    assert "thread/resume" not in fake.methods()
    assert ask_of(store, a) is None


async def test_an_async_answer_typed_in_codex_clears_the_ask(codex):
    broker, store, fake, a = codex
    fake.status = IDLE
    fake.turns[0]["items"] = [async_question()]
    await watch(broker, a, "request_user_input_async")
    await eventually(lambda: ask_of(store, a))
    reply = {"type": "text", "text": async_reply(ITEM, "Which fruit?", "Apple")}
    fake.turns.append({"id": "turn-2", "status": "completed"})
    fake.turns[-1]["items"] = [{"type": "userMessage", "id": "u", "content": [reply]}]
    await settled(broker)
    assert ask_of(store, a) is None
    assert not {"turn/start", "turn/steer", "thread/resume"} & set(fake.methods())


async def test_an_async_question_on_an_unloaded_thread_stops(codex):
    broker, store, fake, a = codex
    fake.status = IDLE
    fake.turns[0]["items"] = [async_question()]
    await watch(broker, a, "request_user_input_async")
    await eventually(lambda: ask_of(store, a))
    fake.status = {"type": "notLoaded"}
    await settled(broker)
    assert ask_of(store, a) is None
    assert not {"turn/start", "turn/steer", "thread/resume"} & set(fake.methods())


async def test_codex_rpc_skips_a_server_request_with_a_colliding_id():
    async def server(ws):
        async for text in ws:
            msg = json.loads(text)
            if "id" in msg:
                await ws.send(json.dumps({"id": msg["id"], "method": "item/tool/requestUserInput"}))
                await ws.send(json.dumps({"id": msg["id"], "result": {"ok": msg["method"]}}))

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "c.sock"
        async with unix_serve(server, path), CodexRPC(str(path)) as rpc:
            assert await rpc.request("thread/read", {}) == {"ok": "thread/read"}


@pytest.mark.parametrize(
    "event,tool,watched",
    [
        ("PreToolUse", "request_user_input", True),
        ("PostToolUse", "request_user_input_async", True),
        ("PostToolUse", "request_user_input", False),
        ("PreToolUse", "request_user_input_async", False),
        ("PreToolUse", "shell", False),
    ],
)
async def test_codex_hook_asks_the_broker_to_watch_its_question(
    environment, monkeypatch, event, tool, watched
):
    state, store = environment
    a = store.register("a", "codex", str(uuid.uuid4()), {})
    write_identity(state, a)
    calls = []

    def watch(self, agent, tool, item_id):
        calls.append((agent["id"], tool, item_id))

    monkeypatch.setattr("agent_wire.questions.CodexQuestions.watch", watch)
    payload = {
        "hook_event_name": event,
        "session_id": a["agent"]["native_id"],
        "tool_name": tool,
        "tool_use_id": ITEM,
    }
    assert await run_hook(state, "codex", payload) == {}
    assert calls == ([(a["agent"]["id"], tool, ITEM)] if watched else [])
