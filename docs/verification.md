# Initial verification

Checked on 2026-09-29 with Python 3.14 on Linux, Codex app-server 0.159.0,
Claude Code 2.1.280, MCP Python SDK 2.2.0, and websockets 17.1.

The automated suite exercises native protocol fixtures, real Unix sockets,
MCP in-memory and stdio clients, CLI subprocesses, persistence across broker
restarts, single broker ownership, private file modes, registration rotation,
message authorization, idempotency, bounded inbox pages, receipt races,
offline recovery, and uncertain delivery without replay.

A separate live test enrolled an existing Codex thread and an isolated Claude
Code print-mode session with only Agent Wire's MCP tools available. The broker
submitted a Codex-origin message through Claude's real peer inbox. Claude
started processing it, but its account reported a weekly usage limit, so a
model-generated reply could not be verified.

A test client then used the Claude fixture's **actual bound MCP server** to
acknowledge that message and send a correlated, explicitly synthetic reply.
The reply arrived in the running Codex conversation as
`agent_wire.message_receive` tool output. Codex acknowledged receipt; the
broker recorded the original as `replied` and the response as `acknowledged`.
The temporary enrollments were retired and fixture processes stopped afterward.
No user conversation was cleared.

This verifies both native transport endpoints and the shared MCP return path.
It does **not** establish a successful autonomous Claude model reply, native
compatibility on macOS, or compatibility with other runtime versions. Those
are useful follow-up contributions; keep the distinction in test reports.

To repeat a live test, use a fresh private state directory and explicit
session enrollment. Configure the receiving runtime's normal permissions for
that test session only. Ask it to acknowledge and send one correlated reply,
then inspect status from both sides. Do not publish credentials, native
session IDs, conversation logs, or unredacted registry records.
