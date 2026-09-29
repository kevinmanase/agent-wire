---
name: herdr-labels
description: Keep this coding agent's Herdr tab label current with its task, stage, and pending questions or actions. Use inside Herdr when HERDR_ENV=1, or when asked to maintain Herdr tab names.
---

Use the bundled `scripts/herdr_tab.py` with Python 3.11+. Resolve its absolute
path beside this skill. It acts only when `HERDR_ENV=1` and `HERDR_PANE_ID`
identifies this pane. Outside Herdr, continue the user's work normally.

## Name the work

Call `name "<stage emoji> <short task>"` once you know the task, and rename at
meaningful milestones. Aim for about 28 characters; put the topic or ticket
first after the icon. Keep the user's chosen wording when it still fits.

Examples, using the absolute path to the installed helper:

```sh
python3 /path/to/herdr-labels/scripts/herdr_tab.py name "🔍 login bug"
python3 /path/to/herdr-labels/scripts/herdr_tab.py name "🛠️ login fix"
python3 /path/to/herdr-labels/scripts/herdr_tab.py name "👀 login review"
```

Stages: 🔍 investigating, 🛠️ building, 🧪 testing, 👀 reviewing, 🚀 PR open,
🎉 merged, 🚧 blocked, 💥 failing, 💤 parked. Use a stage only when it matches
the actual work. Reserve `👑 orchestrator` for the agent the user designated
as the shared supervisor. Naming a tab does not assign that role or launch agents.

## Keep attention visible

| Prefix | Meaning |
| --- | --- |
| ⏳ | Working |
| ✅ | Turn finished, nothing needed from the user |
| ⚪ | Ready, no active task |
| ❓ | The user needs to answer or decide |
| ❗ | The user needs to take an action |

Use `status working` when starting work. Before asking the user or ending a
turn that needs them, put the short, specific ask in the label:

```sh
python3 /path/to/herdr-labels/scripts/herdr_tab.py ask "ship today?"
python3 /path/to/herdr-labels/scripts/herdr_tab.py request "sign in to GitHub"
```

An ask renders as `❓ ship today? · 👀 login review`, preserving the task name.
Renaming during an unresolved ask updates only the task segment.

Keep secrets, raw prompts, and private details out of labels and notifications.
An asynchronous question stays pending even while other work continues. Ordinary
status changes, a new prompt, tool completion, and Stop preserve pending asks.
Call `resolve` only once the user has answered or the action is resolved; it
resumes the working marker with the latest task name. Then rename or set the
appropriate status.

When finishing with nothing needed, use `status done`. This means the turn
ended, not that tests passed, review finished, or a change merged. The helper
leaves conversations open. Hook failures must not block the user's work.

## Agent Wire is separate

Continue publishing your own task reports through Agent Wire's `session_update`
when configured. Its `done` status means the requested work is finished, unlike
the tab's turn-finished checkmark. Use enrolled identities for Agent Wire
messages; a pane ID, label, or crown is never a messaging identity or permission.

The user may use one orchestrator across Claude and Codex workers. Keep your
label and report specific to your assigned part. These naming conventions do
not authorize taking over another agent's work or creating a supervisor.
