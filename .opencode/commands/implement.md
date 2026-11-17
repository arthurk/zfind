---
description: Implement an approved GitHub issue in a temporary worktree and open a PR
agent: architect
subagent: false
---

Implement the approved GitHub issue using the workflow in `AGENTS.md`.

Issue URL or other context:
$ARGUMENTS

If an issue URL is supplied, use it. If no URL is supplied, use the most recent plan issue URL from this conversation. If there is no unambiguous issue URL, ask for it rather than guessing.

Read the issue from GitHub and treat it as the source of truth. Create the temporary worktree under `/tmp/opencode-worktrees`, move this session into it, then run the fresh-context `test-writer`, `implementer`, and `reviewer` subagents sequentially. Preserve their file-ownership boundaries. Verify, commit, push, create a PR, move the session back, and remove the clean worktree without force, following `AGENTS.md` exactly.

Do not merge into the default branch. Report every incomplete step honestly.
