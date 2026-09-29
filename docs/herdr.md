# Optional herdr setup

This is a portable version of the tab naming convention shown in the README.
It adds **status + stage + short task** labels for Claude Code and Codex.
Agent Wire works without it.

The bundle contains:

- [A naming skill](../skills/herdr-labels/SKILL.md) for the agent-maintained task and stage.
- [A Python helper](../skills/herdr-labels/scripts/herdr_tab.py) for labels and optional notifications.
- Hook examples for [Codex](../integrations/herdr/codex-hooks.example.json) and [Claude Code](../integrations/herdr/claude-hooks.example.json).

The helper uses only Python's standard library and the installed `herdr` CLI.
It does not enroll Agent Wire sessions, launch agents, send messages, or clear
conversations. Keep the [Agent Wire MCP and reporting setup](setup.md) alongside
it. Herdr's own session integration also remains separate.

## Install the skill at user scope

Install [herdr](https://github.com/herdrdev/herdr) following its instructions,
and use Python 3.11 or newer. From this repository, copy the skill for the
clients you use:

```sh
codex_skills="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$codex_skills" "$HOME/.claude/skills"
test -e "$codex_skills/herdr-labels" || cp -R skills/herdr-labels "$codex_skills/"
test -e "$HOME/.claude/skills/herdr-labels" || cp -R skills/herdr-labels "$HOME/.claude/skills/"
```

These commands leave an existing `herdr-labels` installation alone. To update
one, compare the files and preserve your customizations. The helper is copied
with the skill, so it does not depend on where you cloned Agent Wire.

Run the coding agent inside a Herdr pane. Herdr supplies `HERDR_ENV=1` and
`HERDR_PANE_ID`. The helper resolves that pane's current tab on every call.
It never targets the focused tab or uses an inherited tab ID as a fallback.
A cross-workspace move can change the public pane ID; if the inherited ID no
longer resolves, the hook leaves labels alone. Tabs containing multiple coding-agent panes
still share one label; use one agent per tab for this naming convention.

## Add lifecycle hooks

Use the example JSON for your client. Replace the placeholder with the absolute
path to your installed helper. If `python3` or `herdr` is not available in the
hook process's PATH, use an absolute Python path in the command and export
`HERDR_BIN_PATH=/absolute/path/to/herdr` before launching the agent.

Merge the hook entries into the existing `hooks` object in:

- Codex: `$CODEX_HOME/hooks.json`, normally `~/.codex/hooks.json`.
- Claude Code: `~/.claude/settings.json`.

Append to existing event arrays. Do not replace the settings file or remove
Agent Wire's hooks. If you already have a custom helper that renames the same
tabs, choose one naming helper for those tabs so they do not overwrite each
other's labels.

Review the new definitions in `/hooks`. Codex requires trusting new or changed
definitions there. Claude's `/hooks` displays the configuration; settings edits
normally reload through its file watcher after workspace trust. Resume or
restart the client if needed to load its configuration. Clearing a conversation
is not an installation step.

| Event | Label behavior |
| --- | --- |
| SessionStart, startup or clear | Reset to `⚪ ready`; preserve a crowned orchestrator name |
| SessionStart, resume, compact, or Claude fork | Keep the current name and pending ask |
| UserPromptSubmit | Mark working, preserving any unresolved ask |
| PreToolUse, question tool | Flag a question without replacing an existing specific ask |
| PermissionRequest | Flag an action without approving the permission |
| Stop | Mark the turn finished, preserving any unresolved ask |

Other tool events do not resolve questions. Claude child hooks with `agent_id`
are ignored so they do not rename the parent's tab. Hooks fail open if Herdr
is unavailable, a response is invalid, or a command times out. Explicit helper
commands report errors for troubleshooting. No label or notification calls run
outside Herdr. Hook output is `{}` with no permission or blocking decisions.

Hook references: [Codex](https://learn.chatgpt.com/docs/hooks) and
[Claude Code](https://code.claude.com/docs/en/hooks).
Herdr command shapes were checked with 0.9.1 and its
[socket API documentation](https://herdr.dev/docs/socket-api/).

## Try the labels

Inside a Herdr pane, use the path for the skill you installed. For a default
Codex installation:

```sh
helper="${CODEX_HOME:-$HOME/.codex}/skills/herdr-labels/scripts/herdr_tab.py"
python3 "$helper" name "🔍 login bug"
python3 "$helper" status working
python3 "$helper" name "🛠️ login fix"
python3 "$helper" ask "ship today?"
python3 "$helper" status done
```

The label becomes `❓ ship today? · 🛠️ login fix`. The question remains visible
after `status done`. Renaming while it is pending changes only the task segment.
Once the user answers:

```sh
python3 "$helper" resolve
python3 "$helper" name "🧪 login tests"
python3 "$helper" status done
```

`resolve` explicitly clears the ask and resumes the working marker with the
latest task name. Use
`request "sign in to GitHub"` for an action instead of a question. Ordinary
status commands preserve pending asks, including when a new user prompt
arrives. The agent should resolve them only when answered or completed.

To enable Herdr notifications for new questions and action requests, set
`HERDR_LABEL_NOTIFICATIONS=1` in the environment before launching the coding
agent. Labels work with notifications off. A failed notification does not undo
the label update. Notifications fire when a tab first enters the question or
action state; renames and repeated hooks do not repeat them.

Tell the agent to use the `herdr-labels` skill. Hooks handle turn state; the
skill tells it to name the task, update the stage, and make asks specific.
The portable helper does not read transcripts or use a model to classify prose.
Plain-text questions need an explicit `ask` or `request` call.

## One orchestrator

Choose one existing agent as your main conversation and tell it that it is the
shared orchestrator. In its own pane, it can name the tab:

```sh
python3 "$helper" name "👑 orchestrator"
```

Give workers narrow tasks, with their own names such as `🛠️ login fix` and
`👀 login review`. They can keep their own runtimes and tools. The orchestrator
reads Agent Wire reports, coordinates enrolled peers, and brings decisions
back to you through its normal conversation.

`✅` only means a turn ended with nothing needed from you. Agent Wire's `done`
report means the agent says its assigned work is finished. Tests and review
establish the result. A tab name is neither a session identity nor evidence
that a task is complete.

## Remove the optional setup

Remove only the hook entries pointing to `herdr-labels`, then remove the copied
skill directory if you no longer need it. Leave Herdr's own integrations and
Agent Wire's hooks and MCP configuration in place.
