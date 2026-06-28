---
name: ai-friend-review
description: >-
  Run multi-AI code reviews with local coding agents. Use when the user says "AI friend review", "ask another AI to review this", "multi-AI review", "get a second AI opinion on this diff", "have other AI coding agents review this", or asks for independent AI review of code, plans, diffs, commits, PRs, or implementation work.
---

# AI Friend Review

Use local AI coding agents as independent reviewers, then merge their findings into a single review report. The goal is sharper implementation judgment, not blind voting. Treat every external reviewer as a source of leads that the primary agent must verify.

## Quick Start

1. Locate this skill directory. If unavailable from context, search known skill roots for `ai-friend-review/SKILL.md`.
2. Discover available reviewers:

```bash
python3 <skill-dir>/scripts/discover_agents.py
```

Use `--refresh` when the user asks to rediscover tools or when a new AI CLI may have been installed.

3. Before spending external AI usage, tell the user which commands will run and that they may consume paid or quota-limited AI usage.
4. Run a review:

```bash
python3 <skill-dir>/scripts/run_review.py --uncommitted --current-agent codex
```

Use `--dry-run` first when you need to inspect commands without calling reviewers.
By default, the runner uses up to 3 ranked reviewers. Use `--reviewers agy,opencode` to request specific reviewers and `--count 2` to choose how many ranked reviewers to run.

## Review Targets

Default to reviewing uncommitted changes. Use the smallest target that matches the user request:

- `--uncommitted`: staged, unstaged, and untracked work.
- `--base <branch>`: changes from a base branch.
- `--commit <sha>`: one commit.
- `--path <file-or-dir>`: focused path review.

Do not let reviewer agents edit files. Run reviewers in read-only or planning modes where their CLIs support it.

## Reviewer Selection

Prefer AI agents other than the current one. Pass `--current-agent <name>` when known, such as `codex`, `claude`, `agy`, or `opencode`.

Use at least two external reviewers when available. Include the current agent only when the user asks, by passing `--include-self` or `--include-current-agent`, or when fewer than two other reviewers are available.

Supported local CLIs are discovered from PATH:

- `agy` (Antigravity CLI)
- `codex`
- `claude`
- `opencode`
- `cursor` (detected, but not used for headless review unless a later script version supports it)

Default reviewer ranking is `agy, claude, opencode, codex`. Override it without editing the skill by setting `AI_FRIEND_REVIEWER_RANKING`, for example:

```bash
AI_FRIEND_REVIEWER_RANKING=opencode,agy,claude python3 <skill-dir>/scripts/run_review.py --count 2
```

The discovery cache lives at `${XDG_CACHE_HOME:-~/.cache}/ai-friend-review/agents.json`. It stores executable paths, versions, and safe invocation templates only. Never inspect auth files, tokens, shell history, private chat logs, or model transcripts.

## Review Standard

All reviewers receive the same standardized review goal and rubric. The runner only adapts how each CLI is invoked:

- `agy`: `agy --print <short prompt-file instruction> --sandbox`
- `codex`: `codex exec --sandbox read-only <short prompt-file instruction>`.
- `claude`: `claude -p --permission-mode plan <short prompt-file instruction>`
- `opencode`: `opencode run --agent plan --dir <repo> <short prompt-file instruction>`

The full standardized prompt is written to `.ai-friend-review/prompts/` before reviewer execution. This keeps large diffs out of command-line arguments and avoids exposing the full prompt through process listings.

Keep reviewer behavior aligned around the same review standard. Do not tailor different goals per model.

Ask reviewers for findings first. Require:

- Severity: `P0`, `P1`, `P2`, or `P3`.
- Exact file and line when possible.
- Observed evidence from the diff, tests, or code.
- Confidence level.
- No style-only comments unless style creates a real defect.

Severity meanings:

- `P0`: blocks the core workflow, causes data loss, exposes secrets, or creates a critical security issue.
- `P1`: likely user-facing failure, broken core behavior, or serious correctness bug.
- `P2`: meaningful defect, missing validation, fragile behavior, or test gap with realistic impact.
- `P3`: minor defect, unclear edge case, documentation mismatch, or low-risk maintainability issue.

## Organizer Merge

The helper script parses structured reviewer findings, creates a first-pass `Aggregated Findings` section, and keeps raw reviewer outputs below it. The organizing agent remains responsible for final synthesis and verification.

After reviewers finish:

1. Read the generated report.
2. Review the parsed `Aggregated Findings` clusters.
3. Check raw reviewer outputs for anything the parser missed.
4. Verify each cluster directly against the code, diff, tests, or command output before calling it real.
5. Preserve disagreement instead of smoothing it away.

The report labels each parsed cluster:

- `Confirmed by multiple reviewers`
- `Single-reviewer concern`
- `Likely false positive or needs verification`

Do not claim a finding is true until you have verified it directly. If verification is blocked, say what was observed, what was inferred, and what remains unknown. The final response should distinguish reviewer claims from verified findings.

## Research Reference

Read `references/multi-ai-review-research.md` when the user asks why multi-AI review helps, wants talking points, or needs a public explanation of the workflow. Keep explanations practical: multiple agents create independent samples, expose different failure modes, and reduce single-model blind spots, but they still need human or primary-agent verification.
