# Contributing

Agent Wire is an independent community project. Small, well-tested pull
requests are welcome. You do not need permission to fix a bug or improve a
setup example. Open an issue before a large adapter or protocol change so we
can agree on the behavior first.

## Set up and check a change

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

The automated suite runs locally without model accounts. It exercises Unix
sockets, the actual MCP SDK, broker restarts, identity rotation, delivery
uncertainty, and message access. CI covers Python 3.11 and 3.14 on Linux, and
Python 3.14 on macOS. Native runtime compatibility needs a separate live check.

For a pull request, explain the observable problem, your change, and how you
verified it. Include a failing regression test for delivery or identity bugs.
Use synthetic sessions in tests; remove private paths, conversations, tokens,
and runtime registry files from reports. Never commit native vendor binaries
or extracted proprietary source.

## Good places to help

- Test a native adapter against another runtime version and add protocol
  fixtures for any interface changes. Runtime version strings are not gated.
- Implement a supported Claude Channels adapter with explicit user opt-in.
- Add convenient installation and service management for Linux and macOS.
- Design remote delivery with authentication and isolation appropriate to
  distinct users; the current Unix socket is deliberately local.
- Improve acknowledgement UX without confusing receipt with task completion.

Read [the protocol](docs/protocol.md) and [security boundaries](SECURITY.md)
before changing adapters. Keep native session identity checks, explicit
enrollment, permission boundaries, and conservative retry behavior intact.
Conversation clearing is outside this project's scope.

## Community and licensing

Be respectful, provide reproducible examples, and discuss ideas without
personal attacks. Maintainers may remove abusive or off-topic content.

Contributions are accepted under the repository's **AGPL-3.0-only** license.
By submitting a contribution, you confirm you have the right to contribute it
under that license. There is no separate contributor license agreement and no
copyright assignment. Please keep attribution for third-party work and verify
license compatibility before including it.

AGPL requires source availability in covered cases, not an upstream pull
request. We encourage upstream fixes so everyone benefits from them.
