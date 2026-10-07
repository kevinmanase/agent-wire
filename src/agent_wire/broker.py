# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import contextlib
import fcntl
import json
import os
import signal
from pathlib import Path

from .adapters import NativeAdapters
from .errors import DeliveryUnknown, Offline, WireError
from .paths import MAX_FRAME, private_directory
from .permissions import sender_mode
from .store import Store


class Broker:
    def __init__(self, store: Store, adapters=None):
        self.store = store
        self.adapters = adapters or NativeAdapters()
        self.wake = asyncio.Event()
        self.delivery_lock = asyncio.Lock()

    async def call(self, method: str, params: dict):
        if not isinstance(params, dict):
            raise WireError("invalid_input", "params must be an object")
        if method == "ping":
            return {"service": "agent-wire", "protocol": 1}
        if method == "discover":
            return await self.adapters.discover(**params)
        if method == "register":
            endpoint = await self.adapters.validate(
                params["runtime"],
                params["native_id"],
                params.get("endpoint", {}),
            )
            # Claude's native record names its process; a Codex hook sends its own runtime's.
            pid = endpoint.pop("pid", params.get("pid"))
            result = self.store.register(**{**params, "endpoint": endpoint, "pid": pid})
            self.wake.set()
            return result
        if method == "ask_answer":
            # A person's answer to a dialog. The private Unix socket limits callers to the local
            # user, and no session credential is accepted, so an agent's tools can't send one.
            if "session_handle" in params:
                raise WireError(
                    "forbidden", "Only a person answers an ask; credentials are refused"
                )
            return self.store.answer(**params)
        token = params.get("session_handle")
        rest = {k: v for k, v in params.items() if k != "session_handle"}
        if method == "sessions_list":
            # Read-only local directory: the private Unix socket enforces the OS-user boundary.
            if token is not None:
                self.store.authenticate(token)
            return self.store.sessions(**rest)
        agent = self.store.authenticate(token)
        if method == "session_refresh":
            if not {"endpoint", "cwd"} <= set(rest) <= {"endpoint", "cwd", "pid"}:
                raise WireError("invalid_input", "session_refresh needs endpoint and cwd")
            endpoint = await self.adapters.validate(
                agent["runtime"], agent["native_id"], rest["endpoint"]
            )
            pid = endpoint.pop("pid", rest.get("pid"))
            return self.store.refresh_endpoint(token, endpoint, rest["cwd"], pid)
        if method == "ask_poll":
            # A dialog hook polls this every second; it has nothing for the delivery worker.
            return self.store.ask_poll(token, **rest)
        if method == "agents_list":
            if rest:
                raise WireError("invalid_input", "agents_list accepts only a session credential")
            return {"agents": self.store.agents()}
        methods = {
            "session_update": self.store.session_update,
            "session_ask": self.store.session_ask,
            "session_heartbeat": self.store.session_heartbeat,
            "message_send": self.store.send,
            "messages_read": self.store.inbox,
            "message_ack": self.store.ack,
            "message_status": self.store.status,
            "retire": self.store.retire,
        }
        if method not in methods:
            raise WireError("unknown_method", "Unknown method")
        result = methods[method](token, **rest)
        self.wake.set()
        return result

    async def deliver_pending(self):
        async with self.delivery_lock:
            slots = asyncio.Semaphore(4)

            async def deliver_one(row):
                async with slots:
                    await attempt(row)

            async def attempt(row):
                self.store.expire()
                agent = self.store.agent(row["recipient"])
                if not agent["active"]:
                    self.store.transition(
                        row["id"], "failed", "Recipient enrollment retired", expected="queued"
                    )
                    return
                if not self.store.transition(row["id"], "delivering", expected="queued"):
                    return
                try:
                    mode = None
                    if agent["runtime"] == "claude":
                        mode = await asyncio.to_thread(sender_mode, self.store.agent(row["sender"]))
                    await self.adapters.deliver(agent, self.store.envelope(row), mode)
                except Offline as exc:
                    self.store.transition(row["id"], "queued", str(exc))
                except DeliveryUnknown as exc:
                    self.store.transition(row["id"], "unknown", str(exc))
                except WireError as exc:
                    self.store.transition(row["id"], "failed", f"{exc.code}: {exc}")
                except asyncio.CancelledError:
                    self.store.transition(row["id"], "unknown", "Delivery interrupted")
                    raise
                except Exception:
                    self.store.transition(
                        row["id"], "unknown", "Unexpected delivery error; not retrying"
                    )
                else:
                    detail = None
                    if agent["runtime"] == "claude" and mode is None:
                        detail = (
                            "Sender permission class unavailable; Claude may hold this message "
                            "for review. Native submission is not a receipt."
                        )
                    self.store.transition(row["id"], "submitted", detail)
                if self.store.message(row["id"])["status"] != "queued":
                    self.wake.set()

            rows = self.store.next_delivery()
            # One process check per pass; attempt() fails messages to retired recipients.
            self.store.retire_exited([row["recipient"] for row in rows])
            async with asyncio.TaskGroup() as group:
                for row in rows:
                    group.create_task(deliver_one(row))

    async def worker(self):
        while True:
            self.wake.clear()
            await self.deliver_pending()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except TimeoutError:
                pass

    async def handle(self, reader, writer):
        try:
            async with asyncio.timeout(10):
                line = await reader.readline()
                if len(line) > MAX_FRAME:
                    raise WireError("too_large", "Request exceeds maximum frame size")
                request = json.loads(line)
                if not isinstance(request, dict) or set(request) != {"method", "params"}:
                    raise WireError("invalid_input", "Expected method and params")
            result = await self.call(request["method"], request["params"])
            response = {"result": result}
        except WireError as exc:
            response = {"error": {"code": exc.code, "message": str(exc)}}
        except (ValueError, TypeError, KeyError):
            response = {"error": {"code": "invalid_input", "message": "Invalid request"}}
        except TimeoutError:
            response = {"error": {"code": "timeout", "message": "Request timed out"}}
        except Exception:
            response = {"error": {"code": "internal", "message": "Broker request failed"}}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            async with asyncio.timeout(5):
                await writer.drain()
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def serve(state: Path):
    state = private_directory(state)
    path = state / "broker.sock"
    if len(os.fsencode(path)) > 100:
        raise WireError("path_too_long", "Use a shorter --state path for the Unix socket")
    lock = os.open(state / "broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WireError(
                "already_running", "A broker already owns this state directory"
            ) from exc
        if path.is_symlink():
            raise WireError("unsafe_path", "Broker socket must not be a symlink")
        path.unlink(missing_ok=True)
        old_mask = os.umask(0o077)
        try:
            store = Store(state / "messages.sqlite3")
            broker = Broker(store)
            server = await asyncio.start_unix_server(broker.handle, path=path, limit=MAX_FRAME + 1)
        finally:
            os.umask(old_mask)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        worker = asyncio.create_task(broker.worker())
        try:
            async with server:
                await stop.wait()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            store.close()
            path.unlink(missing_ok=True)
    finally:
        os.close(lock)
