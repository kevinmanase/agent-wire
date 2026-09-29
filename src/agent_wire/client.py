# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import contextlib
import json
from pathlib import Path

from .errors import WireError
from .paths import MAX_FRAME, check_socket


async def call(state: Path, method: str, **params):
    path = str(state / "broker.sock")
    try:
        check_socket(path)
        reader, writer = await asyncio.open_unix_connection(path, limit=MAX_FRAME + 1)
    except OSError as exc:
        raise WireError(
            "broker_unavailable", "Start agent-wire serve with this --state directory"
        ) from exc
    try:
        request = (json.dumps({"method": method, "params": params}) + "\n").encode()
        if len(request) > MAX_FRAME:
            raise WireError("too_large", "Request exceeds maximum frame size")
        writer.write(request)
        await writer.drain()
        async with asyncio.timeout(30):
            response = json.loads(await reader.readline())
        if "error" in response:
            raise WireError(response["error"]["code"], response["error"]["message"])
        return response["result"]
    except (TimeoutError, OSError, ValueError) as exc:
        raise WireError(
            "request_unknown", "Connection failed; check status before retrying a send"
        ) from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
