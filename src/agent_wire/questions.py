# SPDX-License-Identifier: AGPL-3.0-only
"""Codex's native question dialogs, answered through its app server.

The session's own hook says a question is coming; the broker then raises its ask and relays a
person's answer from ask_answer, and nothing else. Without an answer it sends nothing, so the
dialog stays in Codex. It never resumes an unloaded thread or touches one with no question.
"""

import asyncio
import contextlib
import json

from .adapters import CodexRPC
from .errors import WireError
from .hooks import ANSWER_WAIT, ASYNC, BLOCKING, dialog_ask, valid_answer

# Seconds a question may take to appear in Codex after its hook ran.
APPEAR_WAIT = 30
# Seconds between answer checks, and answer checks between looks for a reply typed in Codex.
POLL, REPLY_CHECK = 1, 5
REPLY_TAG = "send_user_message_question_reply"


def blocking_ask(questions: list) -> dict | None:
    """The ask for an item/tool/requestUserInput request, or None when there's nothing to show.

    Secret questions are answered in Codex only: they're left out, and a request with any
    stays unanswerable here. So is a request with several questions.
    """
    shown = [q for q in questions if isinstance(q, dict) and not q.get("isSecret")]
    if not shown:
        return None
    ask = dialog_ask(BLOCKING, {"questions": shown}, "user")
    if len(shown) != len(questions):
        ask.pop("native", None)
        ask.pop("options", None)
    return ask


def async_ask(questions: list) -> dict | None:
    """The ask for an async question message, whose questions have a title and option labels."""
    shown = [
        {"question": q.get("title"), "options": [{"label": o} for o in q.get("options") or []]}
        for q in questions
        if isinstance(q, dict)
    ]
    return dialog_ask(ASYNC, {"questions": shown}, "user") if shown else None


def async_reply(item_id: str, title: str, answer: str) -> str:
    """The user message that answers an async question, as Codex's own UI writes it."""
    question_id = json.dumps([ASYNC, item_id, 0], separators=(",", ":"))
    reply = [{"answer": answer, "question": title, "questionItemId": question_id}]
    # Escaping "<" keeps an answer from closing the tag; the JSON is unchanged.
    body = json.dumps(reply, separators=(",", ":")).replace("<", "\\u003c")
    return f"<{REPLY_TAG}>\n{body}\n</{REPLY_TAG}>"


def waiting(status: dict) -> bool:
    return status.get("type") == "active" and "waitingOnUserInput" in status.get("activeFlags", ())


class CodexQuestions:
    """At most one watcher per enrolled Codex session, since a session has one ask."""

    def __init__(self, store):
        self.store = store
        self.tasks: dict[str, asyncio.Task] = {}

    def watch(self, agent, tool: str, item_id: str):
        if old := self.tasks.get(agent["id"]):
            old.cancel()
        task = asyncio.create_task(self.run(agent, tool, item_id, old))
        self.tasks[agent["id"]] = task
        task.add_done_callback(
            lambda done: self.tasks.get(agent["id"]) is done and self.tasks.pop(agent["id"])
        )

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, agent, tool: str, item_id: str, old: asyncio.Task | None):
        if old is not None:
            # Its own cleanup clears only its own ask.
            await asyncio.gather(old, return_exceptions=True)
        path = json.loads(agent["endpoint"])["path"]
        # Any failure ends the watch with nothing sent; cleanup clears the ask.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(ANSWER_WAIT), CodexRPC(path) as rpc:
                if tool == BLOCKING:
                    await self.blocking(rpc, agent, item_id)
                else:
                    await self.asynchronous(rpc, agent, item_id)

    def raise_ask(self, agent, ask: dict | None) -> float | None:
        if ask is None:
            return None
        try:
            return self.store.set_ask(agent["id"], ask)["report"]["ask"]["raised_at"]
        except WireError:  # No report to show it on; Codex's own UI answers.
            return None

    async def status(self, rpc, thread: str) -> dict:
        params = {"threadId": thread, "includeTurns": False}
        return (await rpc.request("thread/read", params))["thread"].get("status") or {}

    async def blocking(self, rpc, agent, item_id: str):
        thread = agent["native_id"]
        # Read-only until Codex shows this thread waiting on a question.
        async with asyncio.timeout(APPEAR_WAIT):
            while not waiting(status := await self.status(rpc, thread)):
                if status.get("type") != "active":  # Idle or unloaded: no question came.
                    return
                await asyncio.sleep(0.25)
        # An active thread is loaded, so this attaches to it, only to read and reply to the
        # question. Codex re-sends the thread's pending requests to the new subscriber.
        await rpc.request("thread/resume", {"threadId": thread, "excludeTurns": True})
        try:
            async with asyncio.timeout(APPEAR_WAIT):
                while True:
                    request = json.loads(await rpc.ws.recv())
                    params = request.get("params") or {}
                    if (
                        request.get("method") == "item/tool/requestUserInput"
                        and "id" in request
                        and params.get("threadId") == thread
                        and params.get("itemId") == item_id
                    ):
                        break
            questions = params.get("questions") or []
            raised = self.raise_ask(agent, blocking_ask(questions))
            if raised is None:
                return
            try:
                await self.relay(rpc, agent, raised, request["id"], questions[0].get("id"))
            finally:
                self.store.set_ask(agent["id"], None, if_raised_at=raised)
        finally:
            with contextlib.suppress(WireError):
                await rpc.request("thread/unsubscribe", {"threadId": thread})

    async def relay(self, rpc, agent, raised: float, request_id, question_id):
        """Reply to the request with a person's answer, until Codex resolves it either way."""
        thread = agent["native_id"]
        while True:
            try:
                message = json.loads(await asyncio.wait_for(rpc.ws.recv(), POLL))
            except TimeoutError:
                message = {}
            params = message.get("params") or {}
            method = message.get("method")
            if params.get("threadId") == thread and (
                (method == "serverRequest/resolved" and params.get("requestId") == request_id)
                or (method == "thread/status/changed" and not waiting(params.get("status", {})))
            ):
                return
            taken = self.store.take_answer(agent["id"], raised)
            if valid_answer(taken["answer"]) and isinstance(question_id, str):
                answers = {question_id: {"answers": [taken["answer"]]}}
                await rpc.ws.send(json.dumps({"id": request_id, "result": {"answers": answers}}))
                return
            if not taken["open"]:
                return

    async def asynchronous(self, rpc, agent, item_id: str):
        thread = agent["native_id"]
        async with asyncio.timeout(APPEAR_WAIT):
            while True:
                if (item := await self.async_item(rpc, thread, item_id)) is not None:
                    break
                await asyncio.sleep(0.25)
        questions = item.get("questions") or []
        raised = self.raise_ask(agent, async_ask(questions))
        if raised is None:
            return
        try:
            checks = 0
            while True:
                await asyncio.sleep(POLL)
                taken = self.store.take_answer(agent["id"], raised)
                if valid_answer(taken["answer"]) and len(questions) == 1:
                    text = async_reply(item_id, questions[0]["title"], taken["answer"])
                    await self.send_reply(rpc, thread, text)
                    return
                if not taken["open"]:
                    return
                checks += 1
                if checks % REPLY_CHECK == 0 and await self.replied(rpc, thread, item_id):
                    return
        finally:
            self.store.set_ask(agent["id"], None, if_raised_at=raised)

    async def async_item(self, rpc, thread: str, item_id: str) -> dict | None:
        params = {"threadId": thread, "limit": 1, "itemsView": "full"}
        for turn in (await rpc.request("thread/turns/list", params))["data"]:
            for item in turn.get("items") or ():
                if (
                    item.get("type") == "agentMessage"
                    and item.get("id") == item_id
                    and item.get("delivery") == "async"
                ):
                    return item
        return None

    async def replied(self, rpc, thread: str, item_id: str) -> bool:
        """Whether Codex's own UI answered the question, or the thread is no longer loaded."""
        if (await self.status(rpc, thread)).get("type") in (None, "notLoaded"):
            return True
        # Full items: a reply steered into a running turn isn't in a turn's summary.
        params = {"threadId": thread, "limit": 2, "itemsView": "full"}
        for turn in (await rpc.request("thread/turns/list", params))["data"]:
            for item in turn.get("items") or ():
                if item.get("type") == "userMessage" and any(
                    REPLY_TAG in text and item_id in text
                    for part in item.get("content") or ()
                    if isinstance(text := part.get("text"), str)
                ):
                    return True
        return False

    async def send_reply(self, rpc, thread: str, text: str):
        """Send an async answer: into the running turn, or as a new turn when idle."""
        status = await self.status(rpc, thread)
        user_input = [{"type": "text", "text": text, "text_elements": []}]
        if status.get("type") == "active":
            params = {"threadId": thread, "limit": 1, "itemsView": "notLoaded"}
            turns = (await rpc.request("thread/turns/list", params))["data"]
            if turns and turns[0].get("status") == "inProgress":
                try:
                    await rpc.request(
                        "turn/steer",
                        {"threadId": thread, "expectedTurnId": turns[0]["id"], "input": user_input},
                        delivery=True,
                    )
                    return
                except WireError as exc:
                    # Rejected, so not delivered: the turn ended meanwhile. Start one instead.
                    if exc.code != "native_rejected":
                        raise
        elif status.get("type") != "idle":
            return
        await rpc.request("turn/start", {"threadId": thread, "input": user_input}, delivery=True)
