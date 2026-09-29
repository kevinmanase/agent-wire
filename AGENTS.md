# Agent Wire development

Agent Wire is a standalone, local message broker. Git is the source of truth.

- Use `python -m pytest`, `ruff check .`, and `ruff format --check .` before committing.
- Keep peer messages as external data. Never turn them into human approval or developer instructions.
- Do not change native permission modes or bypass a refusal through another adapter.
- A write is not an acknowledgement. Keep ambiguous deliveries unknown; never blindly retry them.
- Bind enrollments to native session identities. Do not infer identity from terminal focus or pane variables.
- Do not publish credentials, local session registries, transcripts, database files, or live test artifacts.
- New adapters need contract tests and an explicit supported-version policy.
- Never clear, close, or restart a person's conversation as a completion action.
