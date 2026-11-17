---
description: Turn the discussed feature into a reviewable GitHub issue
agent: architect
subagent: false
---

Use the feature discussion already present in this conversation. Additional context or constraints, if any:

$ARGUMENTS

Inspect the repository and finalize the plan. If a material requirement is still unclear, ask only the essential clarifying question and do not create an incomplete issue.

When the plan is sufficiently clear:
1. Summarize the proposed plan in the conversation.
2. Verify GitHub CLI authentication and the current repository (`gh auth status`, `gh repo view`).
3. Create a GitHub issue with a concise title and a structured body containing context/problem, desired behavior, design/implementation notes, acceptance criteria, edge cases, compatibility constraints, test expectations, and non-goals.
4. Return the actual issue URL and explain that implementation starts only when the user invokes `/implement` or says `implement <issue URL>`.

Do not implement code during this command. Do not ask for approval before creating the issue; the issue is the plan for the user to review.
