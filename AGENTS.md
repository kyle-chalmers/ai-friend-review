# ai-friend-review Project Instructions

IMPORTANT: Everything in this repo is public-facing. Do not place secrets, PII,
private recording notes, API keys, local auth details, or owner-only operating
notes here. Internal-only material belongs in `.internal/`, which is gitignored.

## What This Is

A portable coding-agent skill in `skills/ai-friend-review/` that discovers local
AI coding agents and runs read-only multi-AI code review passes.

The skill uses one standardized review prompt for prompt-based reviewers. Only
the CLI invocation adapter differs by tool. Greptile uses its native diff review
flow. Reports include a first-pass `Aggregated Findings` section plus raw
reviewer output.

## Layout

- `skills/ai-friend-review/SKILL.md`: portable skill instructions.
- `skills/ai-friend-review/scripts/discover_agents.py`: local CLI discovery and cache.
- `skills/ai-friend-review/scripts/run_review.py`: read-only review runner and report writer.
- `skills/ai-friend-review/references/`: research and workflow references.
- `install.sh`: one-line copy installer.
- `scripts/setup.sh`: local symlink installer.
- `tests/test_scripts.py`: script checks and aggregation tests.

## Editing Guidance

Keep the skill source portable. Do not hardcode local user paths into
`SKILL.md`; put local discovery in scripts or the cache. Keep reviewer behavior
read-only by default. Treat external AI findings as leads until verified.

Gemini CLI is not a reviewer. Devin, Cursor Agent, Greptile, Kiro, and local
Ollama model reviewers are supported when installed and configured. Greptile
uses a native diff review adapter, and Ollama reviewers receive the standardized
prompt over stdin. The `~/.gemini/antigravity-cli/skills` path is kept only for
Antigravity-style skill loading.

## Verification

Before claiming changes work, run:

```bash
python3 -m unittest tests/test_scripts.py
python3 -m py_compile \
  skills/ai-friend-review/scripts/discover_agents.py \
  skills/ai-friend-review/scripts/run_review.py \
  tests/test_scripts.py
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/ai-friend-review
bash -n install.sh scripts/setup.sh
```
