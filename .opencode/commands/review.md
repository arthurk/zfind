---
description: Run an independent fresh-context review of the current diff against a GitHub issue
agent: architect
subagent: false
---

Run an independent review of the current work against this GitHub issue:

$ARGUMENTS

The architect must:
1. Read the issue using `gh issue view`.
2. Determine the actual diff against the repository default branch, including committed, staged, and unstaged changes as applicable.
3. Start a NEW `reviewer` subagent child session with a fresh context. Do not resume or reuse a previous reviewer session.
4. Pass the reviewer the full issue title/body and the actual diff. Do not pass only an implementer's summary.
5. Return the reviewer's findings and explicit verdict unchanged in substance.

The reviewer must not edit files or change Git state.
