# Initial verification

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

To repeat a live test, use a fresh private state directory and explicit
session enrollment. Configure the receiving runtime's normal permissions for
that test session only. Ask it to acknowledge and send one correlated reply,
then inspect status from both sides. Do not publish credentials, native
session IDs, conversation logs, or unredacted registry records.
