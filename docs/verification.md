# Verification

Checked on 2026-09-29 with Python 3.14 on Linux, Codex app-server 0.159.0,
Claude Code 2.1.280, MCP Python SDK 2.2.0, and websockets 17.1.

The automated suite exercises native protocol fixtures, real Unix sockets,
MCP in-memory and stdio clients, CLI subprocesses, persistence across broker
restarts, single broker ownership, private file modes, registration rotation,
message authorization, idempotency, bounded inbox pages, receipt races,
offline recovery, and uncertain delivery without replay.

A live test used the installed Agent Wire package and a persistent user
service. Codex loaded the shared MCP server into an existing conversation.
A fresh Claude Code print-mode session enrolled itself through the installed
SessionStart hook and verified its own credential with `agents_list`.

Codex sent a message with the actual `message_send` MCP tool. The broker
delivered it through Claude's real peer inbox. **Claude's model** called
`message_ack` and generated a correlated `message_send` reply using its own
hook-provided credential. That reply arrived in the running Codex conversation
as `agent_wire.message_receive` tool output. Codex called the MCP
`message_ack` tool; the original message became `replied` and the response
became `acknowledged`.

The Claude fixture had only Agent Wire's MCP tools available and explicitly
accepted peer messages for that test session. The temporary enrollment was
retired and fixture process stopped afterward. The installed broker remained
running. No user conversation was cleared or restarted.

This verifies both native endpoints, real model-generated replies, shared
unbound MCP configuration, and automatic Claude enrollment. The live Codex
session was enrolled explicitly. Its enrollment hook was also checked locally,
reviewed by the user through `/hooks`, and confirmed enabled and trusted by
the running daemon. Native compatibility on macOS and compatibility with
other runtime versions still need live checks.

## Shared work registry (0.2.0)

The expanded suite passes **68 tests**, including ownership of reports,
freshness with a controlled clock, paginated response-size limits, persistence,
retirement, hook identity reuse, resume endpoint refresh, and suppression of
Claude child hooks. Lint, formatting, wheel, and source-distribution builds
also pass locally.

A second live check used the installed 0.2.0 package and an isolated Claude
session with only `sessions_list` and `session_update` enabled. Codex published
its own task through MCP. Claude's model read it, published its own working
and waiting reports using its hook-provided credential, then read Codex's
updated report and published done. Codex read both versions from the same
shared list. The fixture enrollment was retired and only that test process
was stopped afterward. Existing real Claude sessions also began publishing
their own task reports through the installed reporting integration.

The running Codex app-server loaded version 0.2.0 with both new tools, and the
current Codex conversation successfully called `session_update`. All six
installed Codex lifecycle hooks were subsequently confirmed enabled and trusted
after the user's native `/hooks` review, and the running conversation emitted
activity heartbeats. Reports survived the handover from a temporary broker to
the enabled user service. New installations still need their own hook review.
This is a shared list of enrolled, self-reporting sessions, not proof of
coverage of every running process.

To repeat a live test, use a fresh private state directory and explicit
session enrollment. Configure the receiving runtime's normal permissions for
that test session only. Ask it to acknowledge and send one correlated reply,
then inspect status from both sides. Do not publish credentials, native
session IDs, conversation logs, or unredacted registry records.
