# ai-friend-review

A portable coding-agent skill that asks other local AI coding agents to review
your work, then saves their findings in one gitignored report.

## What It Does

AI Friend Review discovers local AI coding CLIs, caches the safe invocation
metadata, and runs read-only review passes against a diff, branch, commit, or
path. It is meant to turn other AI agents into independent reviewers during a
coding session.

The workflow is deliberately evidence-first. External reviewers provide leads,
not truth. The primary agent still verifies findings against the code, tests,
or runtime behavior.

Reports include a first-pass `Aggregated Findings` section that clusters
structured reviewer findings by location and labels multi-reviewer agreement,
single-reviewer concerns, and likely false positives that need verification.

## Supported Reviewers

The discovery script scans `PATH` and caches executable paths, versions, and
safe command templates under `${XDG_CACHE_HOME:-~/.cache}/ai-friend-review/`.

Headless reviewers supported today:

- `agy` (Antigravity CLI)
- `claude`
- `devin`
- `opencode`
- `codex`
- `cursor` (Cursor Agent)
- `greptile` (native branch/diff review)
- `kiro`
- `gemma3` through local Ollama
- `qwen3` through local Ollama
- `llama3` through local Ollama

Gemini CLI is not used as a reviewer. The `~/.gemini/antigravity-cli/skills`
install path is kept only because local Antigravity-style skill loading may use
that directory.

Ollama reviewers are discovered when `ollama` is installed and a matching local
model is available. Discovery prefers these exact default tags, then falls back
to another installed tag with the same base model name:

- `gemma3`: `gemma3:1b`
- `qwen3`: `qwen3:0.6b`
- `llama3`: `llama3:8b-instruct-q2_K`

Model references:

- Gemma 3: https://huggingface.co/blog/gemma3
- Qwen: https://qwen.ai/home
- Llama 3 on Ollama: https://ollama.com/library/llama3

Override local model names without editing the repo:

```bash
AI_FRIEND_OLLAMA_GEMMA3_MODEL=gemma3:4b
AI_FRIEND_OLLAMA_QWEN3_MODEL=qwen3:4b
AI_FRIEND_OLLAMA_LLAMA3_MODEL=llama3:8b
```

Greptile is a native review adapter. It reviews repository diffs through
`greptile review` instead of reading the shared prompt file. Use it with
`--base` after committing branch changes; it is not used for `--uncommitted`,
`--path`, or `--commit`. Kiro support uses the `kiro-cli-chat` binary and
assumes Kiro CLI authentication is already configured.

If an auto-ranked reviewer cannot handle the chosen target, the runner skips it
with a notice and continues with the remaining reviewers. If you explicitly
request that reviewer with `--reviewer` or `--reviewers`, the runner exits so
the mismatch is visible.

## Install

### Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/kyle-chalmers/ai-friend-review/main/install.sh | bash
```

This installs the skill into `~/.agents/skills/ai-friend-review` and links it
into common local coding-agent skill directories:

- Claude Code: `~/.claude/skills/ai-friend-review`
- Codex: `~/.codex/skills/ai-friend-review`
- Antigravity-style skills: `~/.gemini/antigravity-cli/skills/ai-friend-review`

It also creates lightweight command wrappers for OpenCode and Cursor when their
global command directories are available or can be created.

### Contributor Install

```bash
git clone https://github.com/kyle-chalmers/ai-friend-review ~/Development/ai-friend-review
cd ~/Development/ai-friend-review
./scripts/setup.sh
```

Contributor install symlinks the local skill into the same global locations, so
edits to the repo take effect in new agent sessions.

## Usage

Invoke the skill by name:

```text
AI friend review my current changes
```

Or run the helper directly:

```bash
python3 skills/ai-friend-review/scripts/discover_agents.py --refresh
python3 skills/ai-friend-review/scripts/run_review.py --uncommitted --current-agent codex
```

If no target flag is provided, `run_review.py` defaults to `--uncommitted`.

Review targets:

```bash
python3 skills/ai-friend-review/scripts/run_review.py --uncommitted
python3 skills/ai-friend-review/scripts/run_review.py --base main
python3 skills/ai-friend-review/scripts/run_review.py --commit abc1234
python3 skills/ai-friend-review/scripts/run_review.py --path skills/ai-friend-review
```

Choose exact reviewers or a count:

```bash
python3 skills/ai-friend-review/scripts/run_review.py --reviewers agy,opencode --count 2
python3 skills/ai-friend-review/scripts/run_review.py --count 2
python3 skills/ai-friend-review/scripts/run_review.py --count 4 --include-self
```

By default, the runner uses up to 3 ranked reviewers. Default ranking is
`agy, claude, devin, opencode, codex, cursor, greptile, kiro, gemma3, qwen3, llama3`.
Override it with `AI_FRIEND_REVIEWER_RANKING=opencode,cursor,agy` when you want
a different preference without editing the skill.

Some reviewers only support specific targets. For example, Greptile is eligible
for `--base` reviews only. Ranked selection skips incompatible reviewers, while
explicit reviewer selection fails fast so you know the requested reviewer did
not run.

Install locations can be overridden with environment variables:

```bash
AI_FRIEND_SHARED_SKILLS_DIR=~/.agents/skills
AI_FRIEND_REPLACE_EXISTING=1
ANTIGRAVITY_SKILLS_DIR=~/.gemini/antigravity-cli/skills
CLAUDE_SKILLS_DIR=~/.claude/skills
CODEX_SKILLS_DIR=~/.codex/skills
```

Dry-run the planned reviewer calls first:

```bash
python3 skills/ai-friend-review/scripts/run_review.py --dry-run --uncommitted --current-agent codex
```

Reports are written under `.ai-friend-review/reviews/` in the current repo. That
folder is ignored by this project and should be ignored in downstream repos
when reports are local working artifacts.

## Report Format

Each report includes:

- `Commands`: the exact read-only reviewer commands that ran.
- `Aggregated Findings`: a conservative parser pass over structured reviewer
  findings.
- `Aggregation Notes`: the organizer checklist for verification.
- `Reviewer Outputs`: raw output from each reviewer.

The aggregation labels mean:

- `Confirmed by multiple reviewers`: more than one reviewer reported the same
  likely issue. Still verify before acting.
- `Single-reviewer concern`: one reviewer reported it. Treat it as a lead.
- `Likely false positive or needs verification`: low-confidence or otherwise
  unresolved. Check raw output and code before deciding.

The parser is intentionally conservative. If a reviewer does not follow the
structured finding format, the report keeps the raw output so the organizing
agent can still inspect it.

## Safety

- Reviewers run in read-only or plan modes where their CLIs support it.
- The full review prompt is written to `.ai-friend-review/prompts/`, then
  reviewer CLIs receive a short instruction that points to that local file.
- The discovery cache stores executable paths, versions, and command templates
  only.
- The scripts do not inspect auth files, tokens, private chat logs, shell
  history, or model transcripts.
- External reviewer calls may consume paid or quota-limited AI usage.
- Review context can include diffs and untracked file contents. Inspect your
  changes for secrets before sending them to external reviewer tools.
- Binary-looking untracked files are skipped from prompt context.

## Verification

Run the local checks before publishing changes:

```bash
python3 -m unittest tests/test_scripts.py
python3 -m py_compile \
  skills/ai-friend-review/scripts/discover_agents.py \
  skills/ai-friend-review/scripts/run_review.py \
  tests/test_scripts.py
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/ai-friend-review
bash -n install.sh scripts/setup.sh
```

For an end-to-end dry-run:

```bash
python3 skills/ai-friend-review/scripts/run_review.py \
  --dry-run \
  --uncommitted \
  --current-agent codex \
  --reviewers agy,opencode \
  --count 2
```

## Why Multiple AIs

Multiple reviewers create independent samples. Research on self-consistency,
multi-agent debate, and model ensembling shows that independent generations can
improve coverage and reduce single-model blind spots. Code-review research also
shows the limit: LLM findings still need evidence checks because hallucinations
remain possible.

See `skills/ai-friend-review/references/multi-ai-review-research.md`.
