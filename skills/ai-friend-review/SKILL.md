---
name: ai-friend-review
description: >-
  Run read-only multi-AI code reviews with the local coding-agent CLIs on PATH (codex,
  opencode, devin, claude, local ollama models) and merge their findings into one report,
  with each reviewer's run status recorded so a quota failure never reads as approval.
  Use when the user says "AI friend review", "ask another AI to review this", "multi-AI
  review", "get a second AI opinion on this diff", or asks for independent AI review of
  code, plans, diffs, commits, or PRs. Target arguments (pick one, default --uncommitted):
  --uncommitted, --base BRANCH, --commit SHA, --path FILE_OR_DIR, or --doc FILE to review
  a plan, spec, or doc in full (works outside git). Also --json-out PATH for
  machine-readable per-reviewer status and findings, --reviewers NAMES to choose
  reviewers, --count N, --timeout SECONDS (minimum 120), and --dry-run to print the
  planned commands without spending any AI usage.
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
- `--path <file-or-dir>`: uncommitted changes under one path.
- `--doc <file>`: the full text of a plan, spec, or other document. See below.

`--uncommitted`, `--base`, and `--path` exit with an error when their diff is empty, before any reviewer runs. Reviewers handed nothing to review return the format template unfilled, which used to read as a clean review.

Do not let reviewer agents edit files. Run reviewers in read-only or planning modes where their CLIs support it.

### Reviewing a plan, spec, or doc

Use `--doc`, never `--path`, when the user asks to review a plan or document. `--path` reviews a diff, so a file with no uncommitted changes, or one outside the repository, gives reviewers nothing to see.

```bash
python3 <skill-dir>/scripts/run_review.py --doc ~/.claude/plans/my-plan.md --reviewers codex
```

`--doc` sends the whole file, with line numbers, and swaps the code rubric for a document rubric: wrong or unsupported factual claims, requirement gaps, feasibility and sequencing, risk (security, legal, operational), and internal inconsistency. Findings use the same P0 to P3 format, so the report aggregates them the same way.

The file does not need to be in a git repository. Reviewers run from the current repo when there is one, so they can check the document's claims against the code; otherwise they run from the document's folder, and the prompt and report land in a `.ai-friend-review/` folder beside it. Greptile reviews diffs only and is skipped for `--doc`.

A document longer than the prompt budget (60,000 characters once line numbers are added) is refused rather than cut off, since a reviewer that never saw the end would read as approving all of it. Split it and review each part.

## Reviewer Selection

Prefer AI agents other than the current one. Pass `--current-agent <name>` when known, such as `codex`, `claude`, `agy`, or `opencode`.

Use at least two external reviewers when available. Include the current agent only when the user asks, by passing `--include-self` or `--include-current-agent`, or when fewer than two other reviewers are available.

If an auto-ranked reviewer cannot handle the selected target, the runner skips it with a notice and continues with runnable reviewers. If the user explicitly requested that reviewer through `--reviewer` or `--reviewers`, the runner exits instead so the mismatch is visible.

Supported local CLIs are discovered from PATH:

- `agy` (Antigravity CLI) — opt-in
- `codex`
- `devin`
- `claude`
- `opencode`
- `cursor` (Cursor Agent) — opt-in
- `greptile` (native branch/diff review) — opt-in
- `kiro`
- `ollama` local model reviewers: `gemma3`, `qwen3`, and `llama3`

Default reviewer ranking is `codex, opencode, devin, gemma3, qwen3, claude, llama3, kiro`. codex leads by preference as the primary non-Anthropic reviewer; the rest are ordered by observed reliability rather than capability. `cursor`, `agy`, and `greptile` are **opt-in only** — they are not selected automatically, because account-quota exhaustion and target mismatch make them fail often enough that they crowd out reviewers that would have run. Request them explicitly when you want them:

```bash
python3 <skill-dir>/scripts/run_review.py --reviewers cursor,agy
AI_FRIEND_REVIEWER_RANKING=opencode,codex python3 <skill-dir>/scripts/run_review.py --count 2
```

Ollama model reviewers are discovered when `ollama` is on PATH and a matching local model is installed. Discovery prefers the exact default tags `gemma3:1b`, `qwen3:0.6b`, and `llama3:8b-instruct-q2_K`, then falls back to another installed tag with the same base model name. Override model names with `AI_FRIEND_OLLAMA_GEMMA3_MODEL`, `AI_FRIEND_OLLAMA_QWEN3_MODEL`, and `AI_FRIEND_OLLAMA_LLAMA3_MODEL`.

The discovery cache lives at `${XDG_CACHE_HOME:-~/.cache}/ai-friend-review/agents.json`. It stores executable paths, versions, and safe invocation templates only. Never inspect auth files, tokens, shell history, private chat logs, or model transcripts.

## Reviewer Failure

A reviewer that did not run is not a reviewer that approved. Every result is classified, and the classification appears in the report next to the exit code:

| Status | Meaning | Retried? |
|---|---|---|
| `OK` | Ran and produced output | — |
| `QUOTA` | Account or session limit hit | No, when the CLI states a reset beyond ~60s. Retrying inside a lockout cannot succeed; the reset time is recorded instead. |
| `TIMEOUT` | Exceeded `--timeout` | Yes, twice, backing off 5s then 20s |
| `EMPTY_OUTPUT` | Exit 0 but nothing returned, or only the prompt's finding template copied back unfilled (a literal `<short title>`) | Yes, twice |
| `NOT_INSTALLED` | CLI missing or failed to launch | No |
| `UNSUPPORTED_TARGET` | Adapter cannot review this target | No |
| `ADAPTER_ERROR` | Bad flags or an unrecognized failure | No |

### CLI too old for its configured model

`ADAPTER_ERROR` on a reviewer that is definitely installed usually means its CLI is
older than the model it is pinned to. Observed with codex:

```
ERROR: The 'gpt-5.6-terra' model requires a newer version of Codex.
       Please upgrade to the latest app or CLI and try again.
```

The reviewer launches fine and exits non-zero, so it is not `NOT_INSTALLED` — the model
request is rejected server-side. Two remedies, in order:

```bash
brew upgrade --cask codex     # then re-check: codex --version
```

If upgrading is not an option, pin the model down instead — `model` in
`~/.codex/config.toml`. Verify either fix with a cheap round trip before spending a real
review, since a preflight `--version` probe passes in both the working and broken state:

```bash
echo "Reply with exactly: OK" | codex exec --sandbox read-only -
```

Note there is no `timeout` binary on stock macOS, so do not wrap that check in one.

A preflight probe checks each reviewer's CLI before spending a full review, so a dead roster is reported in seconds instead of after the first reviewer burns its timeout. Skip it with `--skip-preflight`.

`--timeout` has a floor of 120s. A reviewer given too little time reports a timeout indistinguishable from a hung CLI — one run at `--timeout 15` killed four reviewers at once and read as flakiness.

Every run appends per-reviewer outcomes to `${XDG_CACHE_HOME:-~/.cache}/ai-friend-review/reliability.jsonl`, which is what makes the ranking above evidence rather than opinion.

### Long runs and ad-hoc CLI calls

A full review can outlast a foreground shell command. Each reviewer gets `--timeout` (default 900s) and up to two retries, and reviewers run one after another. Claude Code's Bash tool stops a foreground command after 2 minutes by default and 10 at most, so launch the runner as a background command there and read the report when it exits. Use the equivalent in other harnesses.

When calling a reviewer CLI directly instead of through the runner, close its stdin. `codex exec` (observed on 0.160) appends piped stdin to the prompt and waits for EOF, and an agent's shell tool may never close the stdin it hands a command, so the call hangs while printing `Reading additional input from stdin...`:

```bash
codex exec --sandbox read-only --skip-git-repo-check -o /tmp/answer.md "<prompt>" < /dev/null
```

The runner already gives every reviewer a closed stdin, except Ollama reviewers, which receive the prompt over stdin on purpose.

## Machine-Readable Output

`--json-out PATH` writes the run as structured JSON for downstream gates: per reviewer an `id`, `status`, `exit_code`, `attempts`, `error_class`, `error_text`, `reset_at`, and parsed `findings`, plus the target's `head_sha` and `base_sha`.

```bash
python3 <skill-dir>/scripts/run_review.py --base main --json-out /tmp/review.json
```

This exists so a consumer can answer "did codex actually run?" by reading a field rather than grepping prose. The process exits 0 only when every reviewer returned `OK`, and 2 otherwise.

## Review Standard

Prompt-based reviewers receive the same standardized review goal and rubric. The runner only adapts how each CLI is invoked:

- `agy`: `agy --print <short prompt-file instruction> --sandbox`
- `codex`: `codex exec --sandbox read-only --skip-git-repo-check --output-last-message <run-dir file> <short prompt-file instruction>`. The runner parses the last-message file in `.ai-friend-review/outputs/` rather than stdout, where codex prints its final message twice. `--skip-git-repo-check` lets `--doc` reviews run outside git; the read-only sandbox already covers what that check protects.
- `devin`: `devin -p --permission-mode auto --sandbox --prompt-file <prompt-file>`
- `claude`: `claude -p --permission-mode plan <short prompt-file instruction>`
- `opencode`: `opencode run --agent plan --dir <repo> <short prompt-file instruction>`
- `cursor`: `cursor agent --print --mode plan --sandbox enabled --trust --workspace <repo> <short prompt-file instruction>`
- `kiro`: `kiro-cli-chat chat --no-interactive --trust-tools=fs_read --wrap never <short prompt-file instruction>`
- `greptile`: `greptile review --agent --no-color --branch <base>` for base branch targets. This is a native diff review adapter and does not read the shared prompt file. Use it with `--base` after committing branch changes; it is not used for `--uncommitted`, `--path`, or `--commit`.
- `gemma3`, `qwen3`, `llama3`: `ollama run <model> --nowordwrap` with the full standardized prompt sent over stdin.

The full standardized prompt is written to `.ai-friend-review/prompts/` before reviewer execution. This keeps large diffs out of command-line arguments and avoids exposing the full prompt through process listings.

For Ollama reviewers, the prompt is sent over stdin instead of through a prompt file. For Greptile, the CLI performs its own native diff review. Keep those differences visible in reports.

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
