# Setup

Install Agent Wire as shown in the README. Run `agent-wire serve` in a terminal
or a service you manage. Use the same `--state` directory in all commands and
MCP configurations; the default is `$XDG_STATE_HOME/agent-wire`, falling back
to `~/.local/state/agent-wire`. Existing state directories must have mode 0700.

## Try it without models

Create two mailbox identities. Substitute your own UUIDs if you prefer:

```sh
agent-wire register --runtime mailbox --name alice --session mailbox-alice
agent-wire register --runtime mailbox --name bob --session mailbox-bob
```

Save the two returned `identity_file` paths, then run:

```sh
agent-wire send --identity /path/to/alice.json --to bob \
  --body 'Hello from Alice' --key first-message
agent-wire inbox --identity /path/to/bob.json
agent-wire ack --identity /path/to/bob.json MESSAGE_ID
agent-wire send --identity /path/to/bob.json --to alice \
  --body 'Hello back' --reply-to MESSAGE_ID --key first-reply
agent-wire inbox --identity /path/to/alice.json
agent-wire ack --identity /path/to/alice.json REPLY_ID
agent-wire status --identity /path/to/alice.json MESSAGE_ID
```

The first message should be `replied`, with a `reply_id`. A mailbox endpoint
has no automatic native delivery: consumers explicitly read and acknowledge.

## Native runtime prerequisites

Start the conversations you want to connect. `agent-wire discover` prints
session IDs, current names, sockets, and adapter availability. It does not enroll
every session on the machine. Select the intended conversations explicitly.
Runtime versions do not gate enrollment, reporting, or delivery. Compatibility
depends on the native interfaces below, with live identity and socket checks.

Codex must expose its local app-server control WebSocket socket and keep the
target thread loaded. Discovery looks under
`$CODEX_HOME/app-server-control/app-server-control.sock` (default `~/.codex`).
For another location, use `discover --codex-socket /absolute/socket/path`.
A `--no-daemon` Codex session has no such socket. It can use mailbox mode and MCP
polling, but cannot receive automatic tool-output delivery through this adapter.

For a Codex Desktop/CLI session using that mailbox fallback, register with its
actual Codex thread UUID as `--session`. Agent Wire can then verify its outgoing
permission class from Codex's own local session metadata, even without a loaded
control-socket thread. Existing mailbox enrollments with that UUID work without
re-enrollment. An arbitrary mailbox UUID has no native permission evidence.
The broker must use the same `CODEX_HOME` as the sender (default `~/.codex`).

Claude senders report their permission class through the lifecycle hooks. Codex
sender classes are read from the latest runtime metadata for each delivery.
Known, matching classes avoid Claude's unknown/mismatched-mode hold, subject to
its configured inbound policy. If metadata is unavailable, `message_status` / CLI
`status` explains the possible hold in `detail`; check for a receipt before assuming
delivery. See [permission mapping](protocol.md#claude-code) for supported policies.

Claude must publish a live peer inbox in its session registry. Its existing
cross-session inbound policy decides whether it accepts the peer frame.
Agent Wire does not change that policy. If the recipient rejects inbound
messages, use its normal approval/settings interface or read its Agent Wire
inbox explicitly. Do not bypass it with terminal pasting.

Enroll native sessions using the README's `register` commands. A new
enrollment revokes the old credential for that native session. After a clear,
verify the replacement session ID and enroll it separately. In Claude Code and
`--no-daemon` Codex, enrolling it retires the cleared conversation's
enrollment. On the Codex app server, which plain `codex` uses, the cleared
thread stays enrolled until `agent-wire retire` removes it. Old messages are never redirected to the replacement conversation.

## Bind an MCP server to one conversation

Codex `config.toml` example for a configuration used by just one conversation:

```toml
[mcp_servers.agent_wire]
command = "/absolute/path/to/agent-wire"
args = ["mcp", "--identity", "/private/path/to/codex-identity.json"]
```

Claude MCP JSON for a configuration used by just one conversation:

```json
{
  "mcpServers": {
    "agent_wire": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-wire",
      "args": ["mcp", "--identity", "/private/path/to/claude-identity.json"]
    }
  }
}
```

Merge entries into existing settings; do not overwrite other servers. Use
each client's normal MCP trust/approval UI. Claude can also load a dedicated
file with `--mcp-config /path/to/agent-wire-mcp.json`. Do not commit identities.
With a custom state directory, args begin with
`["--state", "/private/state", "mcp", ...]`.

The tool names are `agents_list`, `sessions_list`, `session_update`,
`message_send`, `messages_read`, `message_ack`, and `message_status`; the client
may prefix them with the MCP server name.
Ask each participant to list recipients, send to the intended name, acknowledge
receipt, and use `in_reply_to` for a useful response. Receipt is not approval
to do work beyond that agent's existing task.

These configurations follow the official [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
and [Claude MCP documentation](https://code.claude.com/docs/en/mcp).

## Shared MCP configuration and opt-in hooks

For a configuration shared by several conversations, omit `--identity` from
the MCP args. Authenticated tool calls then require their own `session_handle`
(`sessions_list` is read-only and needs none). A trusted
SessionStart hook can enroll the actual conversation and supply that handle
privately to its context. No peer message is injected through the hook.

Add this entry under `hooks.SessionStart` in your Codex `hooks.json`:

```json
{
  "matcher": "startup|resume|clear|compact",
  "hooks": [{
    "type": "command",
    "command": "/absolute/path/to/agent-wire hook codex",
    "timeout": 30
  }]
}
```

For Claude, use the same entry under `hooks.SessionStart` in its settings, with
`agent-wire hook claude` as the command. Both wrappers read `session_id` from
the hook's JSON input; inherited terminal or pane variables are not identity.
Review new hook definitions through the client's normal trust interface.
See the [Codex hooks reference](https://learn.chatgpt.com/docs/hooks) and
[Claude hooks reference](https://code.claude.com/docs/en/hooks).

Default names include runtime and full native ID to avoid collisions. Avoid a
fixed `--name` on a global hook used by simultaneous conversations. Existing
identities are reused for the same native session, including resume and
compaction. Context hooks revalidate the native endpoint and refresh it when
a resumed process has a new socket. A genuinely new native ID gets its own
enrollment. Explicit `register` still revokes the previous enrollment.
A failed hook leaves the conversation running. Startup timing may precede
native registry/socket readiness; the first prompt hook retries enrollment.
The hook never changes permissions or starts a broker.

## Shared work reports

The shared list is written by each agent using `session_update` (or the CLI
`report` command), not by inspecting tabs, transcripts, or another agent's
prompt. Each report replaces that identity's previous snapshot; omitted
optional fields are cleared. There is no target ID for writing someone else's
report. Reports survive a broker restart. Explicit re-enrollment starts a new
entry with no inherited report; retired entries disappear from the active list.

Install these additional hooks in **both** clients, keeping other existing
hooks intact. Use the same absolute `agent-wire hook codex` / `hook claude`
command as the SessionStart entry and `timeout: 30`:

| Event | Matcher | Purpose |
| --- | --- | --- |
| `UserPromptSubmit` | Omit | Supply reporting instructions, retry enrollment, flag the old report for update |
| `PreToolUse` | `.*` | Record activity, including question tools |
| `PostToolUse` | `.*` | Record recent tool activity |
| `PermissionRequest` | `.*` | Record waiting activity without deciding permission |
| `Stop` | Omit | Record idle activity without declaring the task done |

For example, merge this into the existing `hooks` object (use `hook claude`
in Claude settings):

```json
{
  "UserPromptSubmit": [{
    "hooks": [{"type": "command", "command": "/absolute/path/to/agent-wire hook codex", "timeout": 30}]
  }],
  "PostToolUse": [{
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "/absolute/path/to/agent-wire hook codex", "timeout": 30}]
  }],
  "Stop": [{
    "hooks": [{"type": "command", "command": "/absolute/path/to/agent-wire hook codex", "timeout": 30}]
  }]
}
```

Add PreToolUse and PermissionRequest using the PostToolUse entry's shape.
Hooks run synchronously, do bounded work, and fail open. Claude child hooks
carrying `agent_id` are ignored because they share the parent's `session_id`.
Codex subagent threads are not enrolled either: they share the parent's
process, so with `--no-daemon` Codex enrolling one would retire the parent.
Review the new Codex definitions in `/hooks`; earlier approval of SessionStart
does not trust newly added events. Existing conversations need their runtime
to load the new MCP tool definitions; use its normal reconnect/reload controls.
Do not clear a conversation as an installation step.

### Answering Claude's question dialogs from outside the terminal (opt-in)

Claude Code's question dialog (`AskUserQuestion`) and plan approval
(`ExitPlanMode`) block the conversation until someone answers in the terminal.
This optional PermissionRequest hook raises the dialog as the session's ask and
waits for a person's answer through Agent Wire. Add it beside the `.*`
heartbeat entry, which stays as it is:

```json
"PermissionRequest": [{
  "matcher": "AskUserQuestion|ExitPlanMode",
  "hooks": [{
    "type": "command",
    "command": "/absolute/path/to/agent-wire hook claude --answer",
    "timeout": 86400
  }]
}]
```

- The ask carries the question as `text`, the dialog's 2 to 4 option labels as
  `options`, and `native: true`. A plan approval asks "Approve the plan?" with
  `Approve` and `Keep planning`. `--ask-to NAME` sets the ask's `to` (default
  `user`).
- Answer it as the local user, with the ask's `raised_at` from
  `agent-wire sessions`:

  ```sh
  agent-wire answer <session> --ask-at <raised_at> -- Purple please
  ```

  For a question, any text is the answer: an option's label or free text, and
  comma-separated labels for a multi-select question. For a plan, `Approve`
  approves the plan as written; any other answer turns it down and passes the
  answer to Claude as feedback, so it keeps planning.
- Whoever answers first wins. When the terminal answers first, the hook sees
  the dialog's result in the transcript, clears its ask, and exits. It also
  exits when its ask is cleared or replaced, when Claude or its parent process
  exits, when it is sent SIGTERM or SIGHUP, when the transcript stays
  unreadable for 60 seconds, or after 23 hours, before the 86400-second
  timeout. An answer that arrives after the terminal answered, or after the
  hook stopped waiting, is not relayed.
- The hook never decides by itself. With no answer, a malformed answer, a
  broker that is down, an error, or a timeout, it prints no decision and the
  dialog stays open in the terminal.
- Version 1 answers one-question dialogs. A dialog with several questions still
  raises an ask listing them all, but without `native`: answer it in the
  terminal.
- The session needs a published report to carry the ask; without one the hook
  steps aside.
- After an outside plan approval, Claude Code 2.1.293 continues in accept-edits
  mode, even when the session was in bypass permissions before plan mode. The
  hook doesn't change modes; switch back with shift+tab if you want bypass.

Tested live on Claude Code 2.1.293. `ask_answer` takes no session credential and
is not an MCP tool, so agents' tools can't answer; see the protocol's Native
dialogs section.

### Answering Codex's question dialogs from outside the terminal

Codex's question tools, `request_user_input` (the question dialog, which
blocks the turn) and `request_user_input_async` (a question that ends the turn
and waits in the session), work with the `PreToolUse` and `PostToolUse` `.*`
heartbeat hooks above; nothing else to install. The session must run on the
Codex app server: the daemon, which plain `codex` uses as of Codex 0.161.0, or
the desktop app.
A `--no-daemon` Codex session can't be reached this way.

- When the hook sees the tool start (`request_user_input`) or finish
  (`request_user_input_async`), it asks the broker to watch that one question.
  The broker reads the thread's status until Codex shows the question, then
  raises the session's ask from Codex's own request: the question as `text`,
  its 2 to 4 option labels as `options`, and `native: true`.
- Answer it the same way as a Claude dialog:
  `agent-wire answer <session> --ask-at <raised_at> -- Pear`. Any text is the
  answer: an option's label or free text.
- For the dialog, the broker attaches to the loaded thread (`thread/resume`
  with `excludeTurns: true`) only to read its pending request and reply to it,
  holding one connection while the question waits. The first answer wins: when
  someone answers in Codex, Codex resolves the request, and the broker clears
  the ask and detaches. An async question's answer goes in as the reply message
  Codex's own UI writes, into the running turn (`turn/steer`) or as a new turn
  (`turn/start`) when idle. When the reply was typed in Codex instead, the
  broker sees it in the thread and clears the ask; it checks again just before
  sending, so an outside answer never adds a second reply.
- The broker never decides. With no answer, a malformed answer, a lost
  connection, an error, or a timeout, it sends nothing and the question stays
  in Codex. An answer is taken once and never resent.
- Version 1 answers one-question requests. Several questions raise an ask
  listing them all, without `native`. A question marked `isSecret` raises no
  ask and is answered in Codex only.
- The session needs a published report to carry the ask. A broker restart
  clears the Codex dialog asks it was watching; the questions stay in Codex.

Tested live on Codex 0.160.1 against a separate app server.

The optional skill uses the same instructions for either client. From this
repository, copy `skills/agent-wire/SKILL.md` into
`~/.codex/skills/agent-wire/SKILL.md` and/or
`~/.claude/skills/agent-wire/SKILL.md`. The MCP server and prompt hooks also
provide the essential reporting guidance, so the skill is not a prerequisite.

```sh
agent-wire sessions --table
agent-wire sessions --fresh --status working
agent-wire report --identity /path/to/own.json \
  --task 'Review transport changes' --status waiting --detail 'Waiting for review'
```

`last_seen` measures the latest report or hook heartbeat. `reported_at` records
the semantic report's age; heartbeats never refresh it. A fresh connection can
still have an old report. `needs_update` flags new prompts until the agent
publishes again. Five minutes without contact marks an entry stale, not done
or definitively offline; long model/tool calls can also go stale. Results
include stale and unreported enrollments by default. Follow `next_after`
with `--after` to page through the full list.

## Troubleshooting

| Observation | Check |
| --- | --- |
| `broker_unavailable` | Start the broker with the same state path |
| `recipient_unavailable` | List enrolled agents; verify the current enrollment UUID |
| `unauthorized` | Use the current session's identity; old registrations are revoked |
| `submitted` without a receipt | Check receiving runtime policy, then explicitly read the inbox |
| `unknown` | Reconcile with inbox/status; do not blindly send a new duplicate |
| `path_too_long` | Choose a shorter private state path for the Unix socket |

Stop the broker with Ctrl-C or SIGTERM. Restarting preserves identities and
messages. After upgrading, restart the broker: hooks run the new code at once,
and an older broker can reject their calls.
`agent-wire retire --identity /path/to/identity.json` removes one enrollment
from the active address book; it does not clear or stop its native
conversation. Agent Wire has no conversation-clearing command.
