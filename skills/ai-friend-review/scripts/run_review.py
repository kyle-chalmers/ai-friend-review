#!/usr/bin/env python3
"""Run read-only multi-AI code reviews and write a combined report."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DISCOVER_SCRIPT = SCRIPT_DIR / "discover_agents.py"
SEVERITY_RE = re.compile(r"\bP[0-3]\b")
# codex leads by preference: it is the primary non-Anthropic reviewer. Its one recorded
# failure was an adapter bug (--uncommitted vs a positional prompt) since rewritten, not a
# quota loss, so its real availability matches opencode's.
# The rest are ordered by observed reliability, not capability. Reviewers that fail most
# often on account quota (cursor, agy) or target mismatch (greptile) are off the default
# roster and must be requested explicitly with --reviewer.
DEFAULT_REVIEWER_RANKING = ["codex", "opencode", "devin", "gemma3", "qwen3", "claude", "llama3", "kiro"]
OPT_IN_REVIEWERS = {"cursor", "agy", "greptile"}
MAX_PROMPT_CONTEXT_CHARS = 60000
MAX_UNTRACKED_FILE_CHARS = 12000
DISCOVERY_TIMEOUT_SECONDS = 30
# A reviewer given too little time reports a timeout that is indistinguishable from a
# hung CLI. One run at --timeout 15 killed four reviewers at once and read as flakiness.
MIN_TIMEOUT_SECONDS = 120
RETRY_BACKOFF_SECONDS = (5, 20)
# Retrying inside a quota lockout cannot succeed. Only retry when the CLI reports a reset
# sooner than this; otherwise record the reset time and stop burning wall clock.
QUOTA_RETRY_MAX_WAIT_SECONDS = 60
PREFLIGHT_TIMEOUT_SECONDS = 15
LEDGER_PATH = Path(
    os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
) / "ai-friend-review" / "reliability.jsonl"

# Status values are the contract consumed by downstream gates. OK means the reviewer ran
# and produced output; everything else means it did not, and must never read as approval.
STATUS_OK = "OK"
STATUS_QUOTA = "QUOTA"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_NOT_INSTALLED = "NOT_INSTALLED"
STATUS_UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"
STATUS_EMPTY_OUTPUT = "EMPTY_OUTPUT"
RETRYABLE_STATUSES = {STATUS_TIMEOUT, STATUS_EMPTY_OUTPUT}

# Matched against combined stdout+stderr. Each pattern is a real message observed from the
# CLI it names; keep them literal enough that a reworded error falls through to
# ADAPTER_ERROR rather than being silently misclassified as something benign.
QUOTA_PATTERNS = (
    r"individual quota reached",
    r"you'?ve hit your usage limit",
    r"you'?ve hit your session limit",
    r"quota exceeded",
    r"rate limit(?:ed| exceeded)",
    r"\b429\b",
    r"insufficient[_ ]quota",
)
NOT_INSTALLED_PATTERNS = (
    r"reviewer launch failed",
    r"command not found",
    r"no such file or directory",
)
ADAPTER_ERROR_PATTERNS = (
    r"cannot be used with",
    r"unrecognized argument",
    r"unknown (?:option|flag|argument)",
    r"invalid (?:option|flag|argument|value)",
    r"usage: ",
)
UNSUPPORTED_TARGET_PATTERNS = (
    r"no committed code changes to review",
    r"there are no changes",
)
# "Resets in 63h51m48s" / "resets in 50m21s" / "resets in 45s"
QUOTA_RESET_RELATIVE_RE = re.compile(
    r"resets? in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.IGNORECASE
)
# "resets 10:30pm (America/Phoenix)" — clock time, no date; treated as a long wait because
# the reset could be up to 24h out and we deliberately do not guess the user's timezone.
QUOTA_RESET_CLOCK_RE = re.compile(r"resets?\s+(\d{1,2}:\d{2}\s*(?:am|pm)?)", re.IGNORECASE)
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
KNOWN_FINDING_LABELS = {
    "severity",
    "location",
    "file",
    "file and line",
    "evidence",
    "observed evidence",
    "confidence",
    "why it matters",
    "rationale",
    "suggested fix",
}


@dataclass
class ReviewCommand:
    name: str
    command: list[str]
    stdin: str | None = None


@dataclass
class ReviewOutcome:
    """One reviewer's result, including the reasons it did not produce one.

    This is the record downstream consumers read. `status` is authoritative: anything
    other than OK means the reviewer did not review, and callers must not treat the
    absence of findings as an absence of problems.
    """

    name: str
    command: list[str]
    status: str = STATUS_OK
    exit_code: int | None = None
    attempts: int = 0
    output: str = ""
    error_text: str = ""
    reset_at: str | None = None
    retries_skipped_reason: str | None = None
    findings: list["Finding"] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def parse_quota_reset(text: str) -> tuple[str | None, float | None]:
    """Return (iso8601 reset timestamp, seconds until reset) parsed from CLI output.

    Returns (None, None) when no reset is stated. A clock-time reset ("resets 10:30pm")
    yields a timestamp of None and a deliberately large wait: the message carries no date
    or timezone, and guessing one would produce a confidently wrong retry decision.
    """
    match = QUOTA_RESET_RELATIVE_RE.search(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(value) if value else 0 for value in match.groups())
        wait = hours * 3600 + minutes * 60 + seconds
        if wait > 0:
            reset_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
            return reset_at.isoformat(), float(wait)
    if QUOTA_RESET_CLOCK_RE.search(text):
        return None, float("inf")
    return None, None


def classify_outcome(exit_code: int, output: str) -> str:
    """Map an exit code plus reviewer output onto a status.

    THE EXIT CODE LEADS. Content patterns are only consulted for a run that actually
    failed, because a reviewer's output contains its REVIEW -- prose about the code under
    review, which routinely quotes the same phrases these patterns look for.

    Observed: codex exited 0 with a full review whose text contained "no such file or
    directory", matched the NOT_INSTALLED pattern, and was reported as never having run.
    A process that exits 0 with output is installed by definition, and its findings were
    silently discarded.

    Ordering within the failure branch still matters: quota is checked before the generic
    adapter patterns because several CLIs print a usage banner alongside their quota error,
    and misreading a lockout as an adapter bug sends it into a retry that cannot succeed.
    """
    if exit_code == 124:
        return STATUS_TIMEOUT
    if exit_code == 127:
        return STATUS_NOT_INSTALLED

    if exit_code == 0:
        if not output.strip():
            return STATUS_EMPTY_OUTPUT
        # A CLI that prints a quota error and still exits 0 would otherwise be recorded as
        # a clean review. Only trust that reading when the output carries no findings at
        # all -- a real review that merely mentions a rate limit must stay OK.
        if _matches_any(output, QUOTA_PATTERNS) and not SEVERITY_RE.search(output):
            return STATUS_QUOTA
        return STATUS_OK

    # Non-zero exit: now the output is diagnostic text, so pattern matching is meaningful.
    if _matches_any(output, QUOTA_PATTERNS):
        return STATUS_QUOTA
    if _matches_any(output, NOT_INSTALLED_PATTERNS):
        return STATUS_NOT_INSTALLED
    if _matches_any(output, UNSUPPORTED_TARGET_PATTERNS):
        return STATUS_UNSUPPORTED_TARGET
    return STATUS_ADAPTER_ERROR


@dataclass
class Finding:
    reviewer: str
    title: str
    severity: str
    location: str
    evidence: str
    confidence: str
    why: str
    fix: str


@dataclass
class FindingCluster:
    key: str
    findings: list[Finding]


def run_capture(command: list[str], cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def git_root() -> Path:
    result = run_capture(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if result.returncode != 0:
        raise SystemExit("AI Friend Review requires a git repository.")
    return Path(result.stdout.strip()).resolve(strict=False)


def git_result(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = run_capture(["git", *args], cwd)
    if result.returncode != 0:
        command = "git " + " ".join(shell_quote(arg) for arg in args)
        output = result.stdout.strip() or "(no output)"
        raise SystemExit(f"{command} failed:\n{output}")
    return result


def git_output(args: list[str], cwd: Path) -> str:
    result = git_result(args, cwd)
    return result.stdout.strip()


def git_output_raw(args: list[str], cwd: Path) -> str:
    result = git_result(args, cwd)
    return result.stdout


def discover_agents(refresh: bool) -> list[dict[str, Any]]:
    command = [sys.executable, str(DISCOVER_SCRIPT), "--json"]
    if refresh:
        command.append("--refresh")
    try:
        result = subprocess.run(
            command,
            cwd=str(Path.cwd()),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise SystemExit(f"Agent discovery timed out after {DISCOVERY_TIMEOUT_SECONDS}s.\n{output}".strip()) from exc
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        raise SystemExit(output)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        stderr = result.stderr.strip()
        detail = f"\nstderr:\n{stderr}" if stderr else ""
        raise SystemExit(f"Agent discovery did not return valid JSON.{detail}") from exc
    return [
        agent
        for agent in data.get("agents", [])
        if agent.get("headless_review") and not agent.get("missing_from_path") and not agent.get("missing_model")
    ]


def reviewer_ranking() -> list[str]:
    configured = os.environ.get("AI_FRIEND_REVIEWER_RANKING")
    if not configured:
        return DEFAULT_REVIEWER_RANKING
    ranked = unique_names([name.strip().lower() for name in configured.split(",") if name.strip()])
    return ranked or DEFAULT_REVIEWER_RANKING


def target_name(args: argparse.Namespace) -> str:
    if args.commit:
        return f"commit-{args.commit[:10]}"
    if args.base:
        return f"base-{safe_slug(args.base)}"
    if args.path:
        return f"path-{safe_slug(args.path)}"
    return "uncommitted"


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug[:48] or "review"


def unique_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            unique.append(name)
            seen.add(name)
    return unique


def repo_relative_path(path: str, root: Path) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.exists():
        tracked = run_capture(["git", "ls-files", "--error-unmatch", path], root)
        if tracked.returncode != 0:
            raise SystemExit(f"--path does not exist or match a tracked file: {path}")
    try:
        return str(candidate.resolve(strict=False).relative_to(root))
    except ValueError as exc:
        raise SystemExit(f"--path must be inside the git repository: {path}") from exc


def bounded_section(title: str, body: str, max_chars: int = MAX_PROMPT_CONTEXT_CHARS) -> str:
    if len(body) <= max_chars:
        return f"{title}\n{body.strip() or '(empty)'}"
    omitted = len(body) - max_chars
    return f"{title}\n{body[:max_chars].rstrip()}\n\n[truncated {omitted} chars]"


def untracked_files(root: Path, pathspec: str | None = None) -> list[str]:
    args = ["ls-files", "--others", "--exclude-standard"]
    if pathspec:
        args.extend(["--", pathspec])
    output = git_output(args, root)
    return [line for line in output.splitlines() if line]


def untracked_file_context(root: Path, files: list[str]) -> str:
    sections: list[str] = []
    remaining = MAX_PROMPT_CONTEXT_CHARS
    for relpath in files:
        if remaining <= 0:
            sections.append("[additional untracked files omitted due to context limit]")
            break
        full_path = root / relpath
        if not full_path.is_file():
            continue
        try:
            limit = min(MAX_UNTRACKED_FILE_CHARS, remaining)
            with full_path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            sections.append(f"## {relpath}\n[unreadable: {exc}]")
            continue
        if is_probably_binary(data):
            sections.append(f"## {relpath}\n[skipped binary-looking file]")
            continue
        snippet = data[:limit].decode("utf-8", errors="replace")
        remaining -= len(data[:limit])
        marker = "" if len(data) <= limit else "\n[truncated after bounded read]"
        sections.append(f"## {relpath}\n```text\n{snippet}{marker}\n```")
    return "\n\n".join(sections)


def is_probably_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    sample = data[:1024]
    textish = sum(byte in b"\n\r\t\b\f" or 32 <= byte <= 126 for byte in sample)
    return textish / len(sample) < 0.70


def target_summary(args: argparse.Namespace, root: Path) -> tuple[str, str]:
    if args.commit:
        diff_command = f"git show --stat --oneline --decorate {args.commit}"
        diff = git_output_raw(["show", "--stat", "--patch", "--decorate", args.commit], root)
        return f"commit {args.commit}", bounded_section(diff_command, diff)
    if args.base:
        diff_command = f"git diff --stat {args.base}...HEAD"
        stat = git_output_raw(["diff", "--stat", f"{args.base}...HEAD"], root)
        diff = git_output_raw(["diff", "--no-ext-diff", f"{args.base}...HEAD"], root)
        return f"changes from {args.base}...HEAD", bounded_section(diff_command, f"{stat}\n\n{diff}")
    if args.path:
        path = repo_relative_path(args.path, root)
        diff_command = f"git diff --stat -- {path}"
        stat = git_output_raw(["diff", "--stat", "--", path], root)
        diff = git_output_raw(["diff", "--no-ext-diff", "--", path], root)
        cached = git_output_raw(["diff", "--cached", "--no-ext-diff", "--", path], root)
        untracked = untracked_file_context(root, untracked_files(root, path))
        return f"path {path}", bounded_section(diff_command, f"{stat}\n\n{diff}\n\n{cached}\n\n{untracked}")

    diff = git_output(["status", "--short"], root)
    stat = git_output(["diff", "--stat"], root)
    cached_stat = git_output(["diff", "--cached", "--stat"], root)
    unstaged_diff = git_output_raw(["diff", "--no-ext-diff"], root)
    staged_diff = git_output_raw(["diff", "--cached", "--no-ext-diff"], root)
    untracked = untracked_file_context(root, untracked_files(root))
    body = "\n".join(
        [
            "git status --short",
            diff or "(no changes)",
            "git diff --stat",
            stat or "(no unstaged diff stat)",
            "git diff --cached --stat",
            cached_stat or "(no staged diff stat)",
            "git diff --no-ext-diff",
            unstaged_diff.strip() or "(no unstaged diff)",
            "git diff --cached --no-ext-diff",
            staged_diff.strip() or "(no staged diff)",
            "untracked file contents",
            untracked or "(no untracked file contents)",
        ]
    )
    return "uncommitted changes", bounded_section("working tree context", body)


def review_prompt(args: argparse.Namespace, root: Path) -> str:
    target, summary = target_summary(args, root)
    return f"""You are an independent code reviewer. Review {target} in this repository:
{root}

Focus on bugs, behavioral regressions, security issues, broken tests, missing validation, and requirement gaps. Do not make edits. Do not give style-only comments unless the style issue creates a real defect.

Severity rubric:
- P0: blocks the core workflow, causes data loss, exposes secrets, or creates a critical security issue.
- P1: likely user-facing failure, broken core behavior, or serious correctness bug.
- P2: meaningful defect, missing validation, fragile behavior, or test gap with realistic impact.
- P3: minor defect, unclear edge case, documentation mismatch, or low-risk maintainability issue.

Return findings first. Format each finding exactly as:

### Finding: <short title>
- **Severity**: P0 | P1 | P2 | P3
- **Location**: <file>:<line or range, or unknown>
- **Evidence**: <observed code, diff, test, or command evidence>
- **Confidence**: Low | Medium | High
- **Why it matters**: <impact>
- **Suggested fix**: <specific fix or verification step>

If there are no actionable findings, say that clearly and mention residual verification risk. Do not invent file paths, line numbers, tests, or command results.

Target summary:
{summary}
"""


def select_reviewers(
    agents: list[dict[str, Any]],
    requested: list[str],
    current_agent: str | None,
    include_current: bool,
    count: int | None,
) -> list[dict[str, Any]]:
    if count is not None and count < 1:
        raise SystemExit("--count must be 1 or greater.")

    current_agent = current_agent.lower() if current_agent else None
    by_name = {agent["name"]: agent for agent in agents}
    if requested:
        requested = unique_names([name.lower() for name in requested])
        missing = [name for name in requested if name not in by_name]
        if missing:
            available = ", ".join(by_name) or "none"
            raise SystemExit(f"Requested reviewer(s) not found: {', '.join(missing)}. Available: {available}.")
        selected = [by_name[name] for name in requested]
        return selected[:count] if count else selected

    rank = reviewer_ranking()
    ranked_names = [name for name in rank if name in by_name]
    # Reviewers held back for reliability reasons are reachable only by explicit request
    # (handled above), so they must not leak back in via this catch-all for unranked names.
    ranked_names.extend(
        name for name in by_name if name not in ranked_names and name not in OPT_IN_REVIEWERS
    )
    selected = [
        by_name[name]
        for name in ranked_names
        if include_current or not current_agent or name != current_agent
    ]
    desired_count = count or 3
    if len(selected) < min(2, desired_count):
        for agent in agents:
            if current_agent and agent["name"] == current_agent and agent not in selected:
                selected.append(agent)
    return selected[:desired_count]


def build_command(agent: dict[str, Any], args: argparse.Namespace, root: Path) -> ReviewCommand | None:
    name = agent["name"]
    executable = agent.get("path") or name
    prompt_file = args.prompt_file
    prompt_file_instruction = (
        "Read the complete AI Friend Review prompt from this local file and follow it exactly: "
        f"{prompt_file}. Do not edit files."
    )
    if name == "agy":
        return ReviewCommand(name=name, command=[executable, "--print", prompt_file_instruction, "--sandbox"])

    if name == "codex":
        command = [executable, "exec", "--sandbox", "read-only", prompt_file_instruction]
        return ReviewCommand(name=name, command=command)

    if name == "devin":
        return ReviewCommand(
            name=name,
            command=[executable, "-p", "--permission-mode", "auto", "--sandbox", "--prompt-file", str(prompt_file)],
        )

    if name == "claude":
        return ReviewCommand(name=name, command=[executable, "-p", "--permission-mode", "plan", prompt_file_instruction])

    if name == "opencode":
        return ReviewCommand(name=name, command=[executable, "run", "--agent", "plan", "--dir", str(root), prompt_file_instruction])

    if name == "cursor":
        return ReviewCommand(
            name=name,
            command=[
                executable,
                "agent",
                "--print",
                "--mode",
                "plan",
                "--sandbox",
                "enabled",
                "--trust",
                "--workspace",
                str(root),
                prompt_file_instruction,
            ],
        )

    if name == "greptile":
        if args.commit or args.path or not args.base:
            return None
        command = [executable, "review", "--agent", "--no-color", "--branch", args.base]
        return ReviewCommand(name=name, command=command)

    if name == "kiro":
        return ReviewCommand(
            name=name,
            command=[
                executable,
                "chat",
                "--no-interactive",
                "--trust-tools=fs_read",
                "--wrap",
                "never",
                prompt_file_instruction,
            ],
        )

    if name in {"gemma3", "qwen3", "llama3"}:
        model = agent.get("model")
        if not model:
            return None
        command = [executable, "run", str(model), "--nowordwrap"]
        if name == "qwen3" and agent.get("supports_thinking_flags"):
            command.extend(["--think=false", "--hidethinking"])
        prompt = getattr(args, "review_prompt", None) or review_prompt(args, root)
        return ReviewCommand(name=name, command=command, stdin=prompt)

    return None


def skip_reason(name: str) -> str:
    if name == "greptile":
        return "supports --base only"
    return "adapter is not available for this target"


def skipped_summary(names: list[str]) -> str:
    return ", ".join(f"{name} ({skip_reason(name)})" for name in names)


def run_reviewer(review_command: ReviewCommand, root: Path, timeout: int) -> tuple[int, str]:
    try:
        if review_command.stdin is None:
            result = run_capture(review_command.command, root, timeout)
        else:
            result = subprocess.run(
                review_command.command,
                cwd=str(root),
                check=False,
                input=review_command.stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return 124, f"Reviewer timed out after {timeout}s.\n{output}".strip()
    except OSError as exc:
        return 127, f"Reviewer launch failed: {exc}"
    return result.returncode, result.stdout.strip()


def run_reviewer_with_retries(
    review_command: ReviewCommand,
    root: Path,
    timeout: int,
    sleeper=time.sleep,
) -> ReviewOutcome:
    """Run one reviewer, retrying only failures a retry can plausibly fix.

    Timeouts and empty output get up to two retries with backoff. Quota lockouts do not:
    the CLI has told us when it resets, and retrying inside that window spends wall clock
    to arrive at the same failure. The skipped retry is recorded rather than hidden, so a
    quota failure still reads as a failure downstream.
    """
    outcome = ReviewOutcome(name=review_command.name, command=list(review_command.command))
    for attempt in range(1, len(RETRY_BACKOFF_SECONDS) + 2):
        exit_code, output = run_reviewer(review_command, root, timeout)
        status = classify_outcome(exit_code, output)
        outcome.attempts = attempt
        outcome.exit_code = exit_code
        outcome.status = status
        if status == STATUS_OK:
            outcome.output = output
            outcome.error_text = ""
            return outcome

        outcome.output = output
        outcome.error_text = output[-2000:]
        if status == STATUS_QUOTA:
            reset_at, wait_seconds = parse_quota_reset(output)
            outcome.reset_at = reset_at
            if wait_seconds is None or wait_seconds > QUOTA_RETRY_MAX_WAIT_SECONDS:
                outcome.retries_skipped_reason = (
                    f"quota lockout; reset_at={reset_at or 'unknown'}"
                )
                return outcome
        elif status not in RETRYABLE_STATUSES:
            outcome.retries_skipped_reason = f"{status} is not retryable"
            return outcome

        if attempt <= len(RETRY_BACKOFF_SECONDS):
            sleeper(RETRY_BACKOFF_SECONDS[attempt - 1])
    return outcome


def preflight(commands: list[ReviewCommand], root: Path) -> dict[str, str]:
    """Probe each reviewer's CLI for liveness before spending a full review on it.

    Cheap (`--version`, a few seconds each) and worth it: without this, a dead roster is
    discovered only after the first reviewer has already burned its full timeout.
    Only NOT_INSTALLED is decided here — quota state is not visible to a version probe.
    """
    results: dict[str, str] = {}
    for item in commands:
        executable = item.command[0]
        try:
            probe = subprocess.run(
                [executable, "--version"],
                cwd=str(root),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # A slow --version is not itself a failure; let the real run decide.
            results[item.name] = STATUS_OK
            continue
        except OSError:
            results[item.name] = STATUS_NOT_INSTALLED
            continue
        results[item.name] = STATUS_OK if probe.returncode == 0 else STATUS_NOT_INSTALLED
    return results


def report_path(root: Path, name: str) -> Path:
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    directory = root / ".ai-friend-review" / "reviews"
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{day}-{safe_slug(name)}.md"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = directory / f"{day}-{safe_slug(name)}-{index}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not find an unused report filename.")


def unique_artifact_path(root: Path, directory_name: str, name: str, suffix: str) -> Path:
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    directory = root / ".ai-friend-review" / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{day}-{safe_slug(name)}{suffix}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = directory / f"{day}-{safe_slug(name)}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find an unused {directory_name} filename.")


def dry_run_prompt_file(root: Path, name: str) -> Path:
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    return root / ".ai-friend-review" / "prompts" / f"{day}-{safe_slug(name)}.txt"


def markdown_fence(text: str) -> str:
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def command_display(command: list[str]) -> str:
    visible = []
    for part in command:
        if "\n" in part or len(part) > 160:
            visible.append(f"<review-prompt:{len(part)} chars>")
        else:
            visible.append(part)
    return " ".join(shell_quote(part) for part in visible)


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def strip_markdown(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^\*+", "", value)
    value = re.sub(r"\*+$", "", value)
    return value.strip()


def parse_labeled_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*[-*]\s+\*\*(.+?)\*\*\s*:\s*(.*)$", line)
    if not match:
        match = re.match(r"^\s*[-*]\s+([^:]+):\s*(.*)$", line)
    if not match:
        return None
    label = strip_markdown(match.group(1)).lower()
    if label not in KNOWN_FINDING_LABELS:
        return None
    return label, match.group(2).strip()


def finding_from_block(reviewer: str, title: str, block_lines: list[str]) -> Finding | None:
    fields: dict[str, str] = {}
    current_label: str | None = None
    for line in block_lines:
        parsed = parse_labeled_line(line)
        if parsed:
            label, value = parsed
            current_label = label
            fields[current_label] = value
            continue
        if current_label and line.strip():
            fields[current_label] = f"{fields[current_label]}\n{line.strip()}".strip()

    severity_match = SEVERITY_RE.search(fields.get("severity", ""))
    if not severity_match:
        return None

    location = fields.get("location") or fields.get("file and line") or fields.get("file") or "unknown"
    return Finding(
        reviewer=reviewer,
        title=title or "Untitled finding",
        severity=severity_match.group(0),
        location=location.strip() or "unknown",
        evidence=(fields.get("evidence") or fields.get("observed evidence") or "").strip(),
        confidence=fields.get("confidence", "unknown").strip() or "unknown",
        why=(fields.get("why it matters") or fields.get("rationale") or "").strip(),
        fix=(fields.get("suggested fix") or "").strip(),
    )


def parse_findings(reviewer: str, output: str) -> list[Finding]:
    findings: list[Finding] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in output.splitlines():
        heading = re.match(r"^\s*#{2,4}\s+Finding:?\s*(.*)$", line, re.IGNORECASE)
        if heading:
            if current_title is not None:
                finding = finding_from_block(reviewer, current_title, current_lines)
                if finding:
                    findings.append(finding)
            current_title = heading.group(1).strip() or "Untitled finding"
            current_lines = []
            continue

        if current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        finding = finding_from_block(reviewer, current_title, current_lines)
        if finding:
            findings.append(finding)

    return findings


def normalize_location(location: str) -> str:
    value = location.lower().strip()
    value = re.sub(r"^`|`$", "", value)
    value = re.sub(r"file://", "", value)
    value = re.sub(r"#l\d+.*$", "", value)
    value = re.sub(r":l?(\d+)(?:-l?\d+)?", r":\1", value)
    value = re.sub(r"\s+", "", value)
    return value or "unknown"


def cluster_key(finding: Finding) -> str:
    location = normalize_location(finding.location)
    if location != "unknown" and re.search(r":\d+", location):
        return location
    if location != "unknown":
        return f"{location}:{safe_slug(finding.title)}"
    return safe_slug(finding.title)


def cluster_findings(outputs: dict[str, str]) -> list[FindingCluster]:
    clusters_by_key: dict[str, FindingCluster] = {}
    for reviewer, output in outputs.items():
        for finding in parse_findings(reviewer, output):
            key = cluster_key(finding)
            if key not in clusters_by_key:
                clusters_by_key[key] = FindingCluster(key=key, findings=[])
            clusters_by_key[key].findings.append(finding)

    clusters = list(clusters_by_key.values())
    clusters.sort(
        key=lambda cluster: (
            -len({finding.reviewer for finding in cluster.findings}),
            min(SEVERITY_ORDER.get(finding.severity, 99) for finding in cluster.findings),
            cluster.key,
        )
    )
    return clusters


def agreement_label(cluster: FindingCluster) -> str:
    reviewers = {finding.reviewer for finding in cluster.findings}
    if len(reviewers) > 1:
        return "Confirmed by multiple reviewers"
    if any(finding.confidence.lower().startswith("low") for finding in cluster.findings):
        return "Likely false positive or needs verification"
    return "Single-reviewer concern"


def summarize_finding(finding: Finding) -> list[str]:
    lines = [
        f"  - `{finding.reviewer}`: {finding.title}",
        f"    - Severity: `{finding.severity}`",
        f"    - Location: `{finding.location}`",
        f"    - Confidence: `{finding.confidence}`",
    ]
    if finding.evidence:
        lines.append(f"    - Evidence: {one_line(finding.evidence)}")
    if finding.fix:
        lines.append(f"    - Suggested fix: {one_line(finding.fix)}")
    return lines


def one_line(value: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 15].rstrip()} [truncated]"


def extract_high_signal(outputs: dict[str, str]) -> list[str]:
    findings: list[Finding] = []
    for name, output in outputs.items():
        findings.extend(parse_findings(name, output))
    findings.sort(key=lambda finding: (SEVERITY_ORDER.get(finding.severity, 99), finding.reviewer, finding.location, finding.title))
    return [
        f"{finding.reviewer}: {finding.severity} {finding.location} {finding.title}"
        for finding in findings[:12]
    ]


def write_report(
    path: Path,
    root: Path,
    args: argparse.Namespace,
    commands: list[ReviewCommand],
    outputs: dict[str, str],
    exit_codes: dict[str, int],
    statuses: dict[str, str] | None = None,
) -> None:
    lines = [
        f"# AI Friend Review: {target_name(args)}",
        "",
        f"- Repo: `{root}`",
        f"- Target: `{target_name(args)}`",
        f"- Generated: `{datetime.now().astimezone().isoformat(timespec='seconds')}`",
        "",
        "## Commands",
        "",
    ]
    for item in commands:
        stdin_note = " (review prompt sent over stdin)" if item.stdin is not None else ""
        lines.append(f"- `{item.name}`: `{command_display(item.command)}`{stdin_note}")

    clusters = cluster_findings(outputs)
    lines.extend(
        [
            "",
            "## Aggregated Findings",
            "",
        ]
    )
    if clusters:
        for index, cluster in enumerate(clusters, start=1):
            reviewers = ", ".join(sorted({finding.reviewer for finding in cluster.findings}))
            lines.extend(
                [
                    f"### {index}. {agreement_label(cluster)}",
                    "",
                    f"- Reviewers: `{reviewers}`",
                    f"- Cluster key: `{cluster.key}`",
                    "",
                ]
            )
            for finding in cluster.findings:
                lines.extend(summarize_finding(finding))
            lines.extend(
                [
                    "",
                    "Verification status: unverified by the primary agent.",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "No structured findings were parsed from reviewer output. Read the raw reviewer outputs below.",
                "",
            ]
        )

    lines.extend(
        [
            "",
            "## Aggregation Notes",
            "",
            "The helper script creates a first-pass aggregation from structured reviewer outputs. The organizing agent must verify and synthesize before acting.",
            "",
            "Organizer merge checklist:",
            "",
            "1. Review the parsed Aggregated Findings clusters.",
            "2. Check raw reviewer outputs for anything the parser missed.",
            "3. Verify each cluster directly against code, diff, tests, or command output before calling it real.",
            "4. Preserve disagreement instead of smoothing it away.",
            "",
            "- Confirmed by multiple reviewers: verify overlapping findings before acting.",
            "- Single-reviewer concern: treat as a lead until independently checked.",
            "- Likely false positive or needs verification: record what remains unknown.",
            "",
            "## Reviewer Outputs",
            "",
        ]
    )

    for name, output in outputs.items():
        fence = markdown_fence(output or "")
        header = [
            f"### {name}",
            "",
            f"Exit code: `{exit_codes.get(name)}`",
        ]
        status = (statuses or {}).get(name)
        if status:
            header.append(f"Status: `{status}`")
            if status != STATUS_OK:
                header.append("")
                header.append(
                    f"**This reviewer did not review.** Treat its silence as unknown, not as approval."
                )
        lines.extend(header + ["", f"{fence}text", output or "(no output)", fence, ""])

    path.write_text("\n".join(lines))


def resolve_shas(args: argparse.Namespace, root: Path) -> tuple[str | None, str | None]:
    """Best-effort (head_sha, base_sha) for the reviewed target.

    Returns None for either value rather than guessing when the target has no meaningful
    base (an uncommitted or path review), so a consumer keying on SHA can tell the
    difference between "not applicable" and "wrong". Every lookup is soft: a repo with no
    commits, or a root commit with no parent, must degrade to None rather than abort a
    review that has already been paid for.
    """

    def soft(*git_args: str) -> str | None:
        result = run_capture(["git", *git_args], root)
        return result.stdout.strip() or None if result.returncode == 0 else None

    head = soft("rev-parse", "HEAD")
    if args.commit:
        return soft("rev-parse", args.commit), soft("rev-parse", f"{args.commit}^")
    if args.base:
        return head, soft("merge-base", args.base, "HEAD")
    return head, None


def build_result_payload(
    args: argparse.Namespace,
    root: Path,
    outcomes: list[ReviewOutcome],
    prompt_file: Path,
) -> dict[str, Any]:
    head_sha, base_sha = resolve_shas(args, root)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "target": target_name(args),
        "head_sha": head_sha,
        "base_sha": base_sha,
        "prompt_file": str(prompt_file),
        "timeout_seconds": args.timeout,
        "reviewers": [
            {
                "id": outcome.name,
                "model": outcome.name,
                "command": command_display(outcome.command),
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "attempts": outcome.attempts,
                "error_class": None if outcome.ok else outcome.status,
                "error_text": outcome.error_text or None,
                "reset_at": outcome.reset_at,
                "retries_skipped_reason": outcome.retries_skipped_reason,
                "findings": [
                    {
                        "title": finding.title,
                        "severity": finding.severity,
                        "location": finding.location,
                        "evidence": finding.evidence,
                        "confidence": finding.confidence,
                        "why_it_matters": finding.why,
                        "suggested_fix": finding.fix,
                    }
                    for finding in outcome.findings
                ],
            }
            for outcome in outcomes
        ],
        "summary": {
            "total": len(outcomes),
            "ok": sum(1 for outcome in outcomes if outcome.ok),
            "failed": sum(1 for outcome in outcomes if not outcome.ok),
            "findings": sum(len(outcome.findings) for outcome in outcomes),
        },
    }


def append_ledger(payload: dict[str, Any]) -> None:
    """Append per-reviewer outcomes to a machine-local reliability log.

    Deliberately outside the repo: this accumulates across every repo reviewed, and it is
    the evidence for whether a reviewer is worth keeping in the default roster. Failure to
    write must never take down a review, so all errors are swallowed.
    """
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as handle:
            for reviewer in payload["reviewers"]:
                handle.write(
                    json.dumps(
                        {
                            "at": payload["generated_at"],
                            "target": payload["target"],
                            "head_sha": payload["head_sha"],
                            "reviewer": reviewer["id"],
                            "status": reviewer["status"],
                            "exit_code": reviewer["exit_code"],
                            "attempts": reviewer["attempts"],
                            "findings": len(reviewer["findings"]),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only multi-AI review. Defaults to --uncommitted when no target is provided.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--uncommitted", action="store_true", help="Review staged, unstaged, and untracked work.")
    target.add_argument("--base", help="Review changes against a base branch.")
    target.add_argument("--commit", help="Review one commit.")
    target.add_argument("--path", help="Review a specific file or directory.")
    parser.add_argument("--current-agent", help="Name of the agent running this skill.")
    parser.add_argument("--include-current-agent", action="store_true", help="Allow the current agent as a reviewer.")
    parser.add_argument("--include-self", action="store_true", help="Alias for --include-current-agent.")
    parser.add_argument("--reviewer", action="append", default=[], help="Limit to a reviewer name. Repeatable.")
    parser.add_argument("--reviewers", help="Comma-separated reviewer names, e.g. agy,opencode.")
    parser.add_argument("--count", type=int, help="Number of reviewers to use after ranking or explicit selection.")
    parser.add_argument("--refresh", action="store_true", help="Refresh local agent discovery first.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without calling reviewers.")
    parser.add_argument("--timeout", type=int, default=900, help=f"Timeout per reviewer in seconds (minimum {MIN_TIMEOUT_SECONDS}).")
    parser.add_argument("--json-out", help="Write a machine-readable result file with per-reviewer status and findings.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the reviewer liveness probe.")
    args = parser.parse_args()

    if args.timeout < MIN_TIMEOUT_SECONDS:
        raise SystemExit(
            f"--timeout must be at least {MIN_TIMEOUT_SECONDS}s. "
            f"Shorter timeouts report as reviewer failures rather than saving time."
        )

    root = git_root()
    agents = discover_agents(args.refresh)
    requested = args.reviewer[:]
    if args.reviewers:
        requested.extend(name.strip() for name in args.reviewers.split(",") if name.strip())
    explicit_requested = set(unique_names([name.lower() for name in requested]))
    include_current = args.include_current_agent or args.include_self
    selected = select_reviewers(agents, requested, args.current_agent, include_current, args.count)
    if not selected:
        raise SystemExit("No headless AI reviewers found. Run discover_agents.py --refresh for details.")

    prompt = review_prompt(args, root)
    prompt_file = dry_run_prompt_file(root, target_name(args))
    if not args.dry_run:
        prompt_file = unique_artifact_path(root, "prompts", target_name(args), ".txt")
        prompt_file.write_text(prompt)
    args.prompt_file = prompt_file
    args.review_prompt = prompt
    commands: list[ReviewCommand] = []
    skipped_explicit: list[str] = []
    skipped_auto: list[str] = []
    for agent in selected:
        command = build_command(agent, args, root)
        if command is None:
            if agent["name"] in explicit_requested:
                skipped_explicit.append(agent["name"])
            else:
                skipped_auto.append(agent["name"])
        else:
            commands.append(command)
    if skipped_explicit:
        raise SystemExit(
            "Reviewer(s) cannot run for this target: "
            f"{skipped_summary(skipped_explicit)}."
        )
    if not commands:
        raise SystemExit("No runnable reviewer commands were built.")

    print("AI Friend Review will run read-only reviewer commands.", flush=True)
    print("These commands may consume paid or quota-limited AI usage.", flush=True)
    print("Review context may include diffs and untracked file contents. Inspect your changes for secrets before running external reviewers.", flush=True)
    print(f"Full review prompt file: {prompt_file}", flush=True)
    for item in commands:
        stdin_note = " (review prompt sent over stdin)" if item.stdin is not None else ""
        print(f"- {item.name}: {command_display(item.command)}{stdin_note}", flush=True)
    if skipped_auto:
        print(f"Skipped reviewer(s): {skipped_summary(skipped_auto)}.", flush=True)

    if args.dry_run:
        return 0

    if not args.skip_preflight:
        probe = preflight(commands, root)
        dead = [name for name, status in probe.items() if status != STATUS_OK]
        if dead:
            print(f"Preflight: reviewer(s) not runnable: {', '.join(sorted(dead))}.", flush=True)
        live = [item for item in commands if probe.get(item.name) == STATUS_OK]
        if not live:
            raise SystemExit(
                "Preflight found no runnable reviewers. "
                "Run discover_agents.py --refresh, or pass --skip-preflight to try anyway."
            )
        commands = live

    outcomes: list[ReviewOutcome] = []
    for item in commands:
        print(f"Running reviewer: {item.name}", flush=True)
        outcome = run_reviewer_with_retries(item, root, args.timeout)
        if outcome.ok:
            outcome.findings = parse_findings(outcome.name, outcome.output)
        else:
            detail = outcome.retries_skipped_reason or f"after {outcome.attempts} attempt(s)"
            print(f"  {outcome.name}: {outcome.status} ({detail})", flush=True)
        outcomes.append(outcome)

    outputs = {outcome.name: outcome.output for outcome in outcomes}
    exit_codes = {outcome.name: outcome.exit_code for outcome in outcomes}

    statuses = {outcome.name: outcome.status for outcome in outcomes}
    path = report_path(root, target_name(args))
    write_report(path, root, args, commands, outputs, exit_codes, statuses)

    payload = build_result_payload(args, root, outcomes, prompt_file)
    append_ledger(payload)
    if args.json_out:
        json_path = Path(args.json_out).expanduser()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"Result JSON written: {json_path}", flush=True)

    print(f"Report written: {path}", flush=True)
    # Say plainly which reviewers ran and which did not. A reviewer that never ran is the
    # failure mode this whole path exists to make visible.
    ran = [outcome.name for outcome in outcomes if outcome.ok]
    failed = [f"{outcome.name} ({outcome.status})" for outcome in outcomes if not outcome.ok]
    print(f"Reviewers completed: {len(ran)}/{len(outcomes)}" + (f" — ran: {', '.join(ran)}" if ran else ""), flush=True)
    if failed:
        print(f"Reviewers that did NOT review: {', '.join(failed)}", flush=True)

    high_signal = extract_high_signal(outputs)
    if high_signal:
        print("High-signal finding lines:", flush=True)
        for line in high_signal:
            print(f"- {line}", flush=True)
    else:
        print("No severity-tagged finding lines were detected. Read the report for full reviewer output.", flush=True)

    return 0 if all(outcome.ok for outcome in outcomes) else 2


if __name__ == "__main__":
    sys.exit(main())
