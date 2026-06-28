# ai-friend-review Project Instructions

IMPORTANT: Everything in this repo is public-facing. Do not place secrets, PII,
private recording notes, API keys, local auth details, or owner-only operating
notes here. Internal-only material belongs in `.internal/`, which is gitignored.

This repo publishes the `ai-friend-review` coding-agent skill. The source of
truth is `skills/ai-friend-review/SKILL.md`; helper scripts live under
`skills/ai-friend-review/scripts/`.

Keep changes small and reviewable. Preserve read-only reviewer behavior, avoid
machine-specific assumptions in public files, and verify helper scripts before
claiming they work.

The current design uses one standardized review prompt for all reviewers and
tool-specific invocation adapters only. Reports include a first-pass
`Aggregated Findings` section, but external AI findings are still leads until
verified against code, tests, or command output.

Run the verification commands listed in `AGENTS.md` before shipping changes.
