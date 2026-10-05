# Security

Agent Wire is alpha software. Use it among sessions you trust on your own
machine. The local OS user is its trust boundary: any process running as that
user can read identity files or enroll an agent. Credentials prevent accidental
sender mix-ups; they are not isolation from a hostile process with the same
filesystem privileges.

## Controls and limits

- The broker listens on a private Unix socket. Its state directory is owned by
  the current user with mode 0700; identities and database files are private.
- Each enrollment has a random credential; SQLite stores its hash. Operations
  use that identity rather than a caller-supplied sender name. Only message
  participants can inspect message state; only its recipient can acknowledge it.
- Work reports can be replaced only with the owning session's credential.
  The read-only session directory is visible to the local OS user and all its
  participating agents. Reports are peer data, not trusted instructions or
  verified task evidence. Staleness does not prove completion or process exit;
  the broker retires an enrollment only when its recorded process has exited.
- Native destinations are explicit session IDs and sockets. The adapter checks
  current runtime state before delivery. It never attaches to historical
  conversations, resets tabs, or reassigns queued messages to a new enrollment.
- Re-enrollment revokes the old credential. Already submitted native messages
  cannot be recalled. Retiring an address is not a native runtime kill switch.
- Peer content stays peer data. The bridge never executes bodies, turns a
  message into permission approval, or uses terminal pasting as a fallback.
- Claude's native inbound rules still apply. A successful socket write is not
  evidence that Claude accepted or read a message.
- Unknown write outcomes are not replayed automatically. Body, inbox, rate,
  expiry, and reply-hop limits constrain accidental loops; they do not make
  mutually hostile agents safe to co-locate.

## Data

Messages, work reports, paths, names, and receipts are stored in local SQLite **without
application-level encryption**. Identities contain private credentials. Native
delivery also places message content in the receiving runtime's conversation
and may send it to that runtime's model provider under your existing setup.
Agent Wire itself does not call a model API or send telemetry.
Agents may send the shared work list to their model provider when reading it.
Hooks do not copy raw prompts or transcripts into reports. Agents should publish
brief summaries without secrets. Reports from retired enrollments remain in
the database even though they are excluded from the active directory.

The alpha does not automatically prune message history. Expiry stops pending
delivery and acknowledgement; it does not erase stored content. Protect state
backups. To remove all bridge data, stop the broker and deliberately remove its
state directory. That does not delete native conversation histories.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/kevinmanase/agent-wire/security/advisories/new)
for a suspected security defect. Include the Agent Wire version, OS, runtime
versions, a minimal reproduction, and the expected boundary. Do not include
real credentials or private conversations. Ordinary bugs belong in public
issues. Security fixes currently target the latest main branch; there is no
long-term support release yet.
