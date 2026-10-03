# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_wire.broker import Broker
from agent_wire.store import Store


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
