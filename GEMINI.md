# ai-friend-review Project Instructions

IMPORTANT: Everything in this repo is public-facing. Do not place secrets, PII,
private recording notes, API keys, local auth details, or owner-only operating
notes here. Internal-only material belongs in `.internal/`, which is gitignored.

This repo publishes the `ai-friend-review` coding-agent skill. Keep the portable
skill in `skills/ai-friend-review/SKILL.md`, and keep local machine discovery in
the bundled scripts or local cache.

Gemini CLI is not a reviewer for this project. The Antigravity-style skill path
under `~/.gemini/antigravity-cli/skills` may still be used for local skill
loading.

Preserve the shared-prompt design: all reviewers receive the same review goal
and rubric, while each CLI gets its own invocation adapter. Reports include
first-pass aggregation, but the organizing agent must verify findings before
treating them as true.

Run the verification commands listed in `AGENTS.md` before shipping changes.
