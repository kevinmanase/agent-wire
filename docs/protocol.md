# Protocol and native adapters

The public API is the seven MCP tools described in the README. The broker's
local protocol is version 1: one newline-terminated JSON request and response
per Unix socket connection. A request is `{"method": "...", "params": {...}}`;
a response is `{"result": ...}` or `{"error": {"code": "...", "message": "..."}}`.
Frames are limited to 1 MiB. This is not a public network endpoint.

## Identities and addresses

An enrollment has a generated UUID, unique active display name, runtime,
native session ID, socket endpoint, and random private credential. Native
session IDs are not credentials. Enrolling a native runtime/session pair again
retires its earlier identity. Names may be reused after retirement, but queued
messages always retain their original destination UUID.

Discovery is read-only. Enrollment is a local-user administrative operation;
it validates that a native target is currently loaded. MCP tools cannot enroll
arbitrary targets. A bound MCP process reads one private identity file. An
unbound process requires the calling conversation's own `session_handle` on
authenticated operation. `sessions_list` is read-only and available without
a session credential through the private socket. Never bind a shared
app-scoped process to one conversation.

## Session reports

`session_update` replaces the authenticated caller's report, with required
`task` and `status` (`working`, `waiting`, `blocked`, `idle`, `done`). Optional
`detail`, `repository`, `branch`, and `ticket` default to empty strings and
replace prior values. Limits in UTF-8 bytes are task 512, detail 2048,
repository 4096, branch 256, and ticket 256. The server sets `reported_at` and
`last_seen` using receipt time; callers cannot choose an owner or timestamps.

`sessions_list` returns active enrollment metadata plus `report` (null until
published), `activity`, `last_seen`, `age_seconds`, and `freshness`
(`unseen`, `fresh`, `stale`). No endpoint, credential, prompt, or transcript is
returned. Filters are `runtime`, `status` (including `unreported`), and
`include_stale` (default true). Pages use an enrollment-ID `after` cursor,
`limit` 1–100 (default 50), and a 512 KiB encoded-entry budget. Follow
`next_after` until null. Concurrent registrations may require a new scan.

Hook-only `session_heartbeat` updates activity and last contact, leaving task
state and `reported_at` intact. `new_turn` marks an existing report as needing
an update; only a new `session_update` clears that marker. `session_refresh`
revalidates the caller's own native endpoint when a context hook runs, allowing
a resumed conversation to keep its identity and report at a new inbox socket.
A heartbeat also records the session's permission class from the hook's
`permission_mode`: `bypass` for `bypassPermissions`, `prompting` for `default`,
`acceptEdits`, `dontAsk`, and `auto`, and none for `plan` or a missing mode,
because plan mode can be either class. Each heartbeat replaces the previous
class, so a hook without a mode clears it rather than leaving it stale.
It cannot select a different native session. These methods are not MCP tools.

Reports are claims by agents. Hooks do not infer summaries or completion, and
the broker does not keep agents fresh on their behalf. At 300 seconds without
contact an entry becomes stale. A stale entry stays visible; it is not proof
of process exit or task completion. A stopped turn sets activity to idle and
preserves a waiting/blocked/done report. Explicit retirement removes the entry
from the active list; re-enrollment never transfers an old report.

## Message lifecycle

Each message has a UUID, global monotonic `seq`, sender and recipient enrollment
records, body, creation/expiry timestamps in Unix seconds, optional
`in_reply_to`, reply-hop count, status, detail, acknowledgement time, and
optional `reply_id`.

| Status | Meaning |
| --- | --- |
| `queued` | Durable; awaiting native submission or mailbox read |
| `delivering` | Native submission is in progress |
| `submitted` | App-server accepted the request, or the Claude socket write completed |
| `acknowledged` | Recipient explicitly recorded receipt |
| `replied` | Recipient submitted a correlated reply; `reply_id` identifies the latest reply |
| `unknown` | Submission may have happened; no automatic replay |
| `expired` | The acknowledgement deadline passed |
| `failed` | A definitive rejection or retired queued destination prevented delivery |

Acknowledgement is not task completion. Sending a reply acknowledges its parent
but does not prove the reply was delivered. Receipts may arrive before the
native write completes; conditional state updates preserve those receipts.
Expired messages are excluded from the inbox; a later explicit reply can
still correlate to an expired parent and records `replied`.

A pre-write offline failure can retry. A possibly completed write cannot.
After a broker crash, `delivering` becomes `unknown`. Use `messages_read` to
recover unknown deliveries and explicitly acknowledge them. A message can
remain `submitted` until expiry when the native runtime silently declines it.
There is no exactly-once processing guarantee. Do not infer human consent
from any transport status.

The sender supplies an idempotency key (the CLI/MCP generates one when omitted).
Reusing it with exactly the same recipient selector, body, reply link, and TTL
returns the original message. Changing those fields is an error. Specify
`--key` / `idempotency_key` before sending if you need safe retries after losing
the response. Reusing a key does not retry an `unknown` native submission.

Reading pending messages does not acknowledge them. A cursor is the final
returned sequence number; pages stop at the count or encoded-size budget.
Continue until a page is empty. Start at zero when recovering unacknowledged
messages, including earlier messages already returned by a previous read.

Defaults: 64 KiB UTF-8 body, 64 pending messages per recipient, 30 sends per
sender per minute, TTL 3,600 seconds (range 1–86,400), and 8 reply hops. Up to
four destinations can deliver concurrently; each sweep selects its oldest
queued message. Polling offline destinations occurs every two seconds.
Runtime processing order is outside the broker's guarantees.

## Codex

The adapter was verified against **app-server 0.159.0**, with an allowlist for
the 0.159 series. This is the app-server version, which may differ from the
installed CLI. It uses the local control socket via WebSocket, initializes the
connection, checks `thread/loaded/list`, and reads the identified thread. It
does not resume an unloaded thread.

Delivery calls `turn/start` with an empty `input` and
`toolOutput: {name: "message_receive", namespace: "agent_wire", output: "..."}`.
The output contains the message envelope plus a peer-data notice. The daemon
can queue it during an existing turn. It is not inserted as developer
instructions, a human message, or hook-supplied peer content.

Codex's native child-agent collaboration primitives address its own agent
tree. Host-specific thread messaging tools are another interface; neither is
the portable API that Claude consumes here. The bridge exposes a common MCP
surface and uses the app-server for inbound delivery.

Primary references: [Codex app-server](https://developers.openai.com/codex/app-server/)
and the JSON schema generated by the installed `codex app-server generate-json-schema`.
Socket discovery and `toolOutput` delivery are version-sensitive extensions;
verify them before widening the allowlist.

## Claude Code

The adapter targets **Claude Code 2.1.280, 2.1.285, and 2.1.286**, peer protocol 1. It reads live,
same-user session registry records under `~/.claude/sessions` (or
`CLAUDE_CONFIG_DIR`), verifies the PID and owned Unix socket, and matches the
native session ID again before every delivery.

The observed inbox accepts newline-delimited JSON with `type: "user"`, the
target `session_id`, an explicit `from: "agent-wire:<sender UUID>"`, `msg_id`,
`priority: "next"`, and `message: {role: "user", content: "<peer envelope>"}`.
The `from` field identifies the peer transport. The receiver's session fence
and inbound controls remain in force. No child-agent token or privileged
origin mode is used. Replies must use Agent Wire's MCP tool, because Codex is
not a native Claude inbox reply address.

Claude holds a peer message for a session that bypasses permission prompts
unless the sender states its own permission class, and holds a message whose
class differs from the recipient's. When the sender's latest heartbeat
recorded a class, the envelope is wrapped the way Claude's own peer messages
are: `<cross-session-message from-mode="bypass|prompting">`, a newline, the
JSON envelope, a newline, and the closing tag. The envelope escapes every `<`
as `\u003c`, so a peer body cannot close the wrapper. With no recorded class,
the envelope is sent bare and Claude's inbound policy decides as before.

This adapter uses an **observed internal protocol**, not a promised stable
Anthropic integration API. It is independent implementation code; no vendor
binary or extracted proprietary source is distributed here. A completed
socket write is only `submitted`: there is no processing acknowledgement on
that connection. Claude team mailboxes are a different mechanism and are not
used. A future [Claude Channels](https://code.claude.com/docs/en/channels)
adapter can offer a supported opt-in integration.

Neither native adapter rewrites global runtime settings, launches a session,
forges user approval, or falls back to pasting into a terminal.
