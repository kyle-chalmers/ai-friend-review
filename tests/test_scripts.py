#!/usr/bin/env python3
"""Unit-style checks for AI Friend Review helper scripts."""

from __future__ import annotations

import contextlib
import json
import importlib.util
import argparse
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVER = REPO_ROOT / "skills" / "ai-friend-review" / "scripts" / "discover_agents.py"
RUN_REVIEW = REPO_ROOT / "skills" / "ai-friend-review" / "scripts" / "run_review.py"

spec = importlib.util.spec_from_file_location("run_review", RUN_REVIEW)
assert spec and spec.loader
run_review = importlib.util.module_from_spec(spec)
sys.modules["run_review"] = run_review
spec.loader.exec_module(run_review)

discover_spec = importlib.util.spec_from_file_location("discover_agents", DISCOVER)
assert discover_spec and discover_spec.loader
discover_agents = importlib.util.module_from_spec(discover_spec)
sys.modules["discover_agents"] = discover_agents
discover_spec.loader.exec_module(discover_agents)


def write_fake_cli(directory: Path, name: str, body: str | None = None) -> None:
    path = directory / name
    reviewer_body = body or f'echo "{name} fake reviewer"'
    path.write_text(
        f"""#!/usr/bin/env sh
if [ "${{1:-}}" = "--version" ]; then
  echo "{name} 1.2.3"
  exit 0
fi
{reviewer_body}
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@contextlib.contextmanager
def stdin_held_open():
    """Point fd 0 at a pipe whose write end stays open, so a child that reads stdin blocks.

    This is the shape of an agent harness that never closes a tool's stdin. Child
    processes started with the default stdin inherit it.
    """
    read_fd, write_fd = os.pipe()
    saved = os.dup(0)
    os.dup2(read_fd, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_fd, write_fd):
            os.close(fd)


# Mimics codex-cli 0.160: it refuses to run outside git without --skip-git-repo-check,
# appends piped stdin to the prompt by reading it to EOF, prints its final message twice,
# and writes the message once to --output-last-message when asked.
FAKE_CODEX_BODY = """out=""
skip=""
prev=""
for arg in "$@"; do
  case "$prev" in -o|--output-last-message) out="$arg" ;; esac
  [ "$arg" = "--skip-git-repo-check" ] && skip=1
  prev="$arg"
done
if [ -z "$skip" ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not inside a trusted directory and --skip-git-repo-check was not specified." >&2
  exit 1
fi
if [ ! -t 0 ]; then
  echo "Reading additional input from stdin..." >&2
  cat >/dev/null
fi
message='### Finding: Step 2 depends on an API that step 3 ships
- **Severity**: P1
- **Location**: plan.md:3
- **Evidence**: Step 2 calls the billing API, which step 3 creates.
- **Confidence**: High
- **Why it matters**: The plan cannot run in the stated order.
- **Suggested fix**: Ship the API before wiring the button to it.'
printf '%s\\n' "$message"
printf '%s\\n' "$message"
if [ -n "$out" ]; then printf '%s\\n' "$message" > "$out"; fi"""

# What codex returned when handed an empty review target: the prompt's format block,
# copied back unfilled.
TEMPLATE_ECHO = """### Finding: <short title>
- **Severity**: P0 | P1 | P2 | P3
- **Location**: <file>:<line or range, or unknown>
- **Evidence**: <observed code, diff, test, or command evidence>
- **Confidence**: Low | Medium | High
- **Why it matters**: <impact>
- **Suggested fix**: <specific fix or verification step>
"""


class ScriptChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.cache = self.root / "cache"
        self.bin.mkdir()
        for name in ["agy", "codex", "devin", "claude", "opencode", "cursor", "greptile", "kiro-cli-chat"]:
            write_fake_cli(self.bin, name)
        write_fake_cli(
            self.bin,
            "ollama",
            """if [ "${1:-}" = "list" ]; then
  cat <<'MODELS'
NAME                       ID              SIZE      MODIFIED
gemma3:1b                  fake            815 MB    now
qwen3:0.6b                 fake            522 MB    now
llama3:8b-instruct-q2_K    fake            3.2 GB    now
MODELS
  exit 0
fi
if [ "${1:-}" = "run" ] && [ "${2:-}" = "--help" ]; then
  echo "Usage: ollama run MODEL [PROMPT] [flags]"
  echo "      --think"
  echo "      --hidethinking"
  exit 0
fi
if [ "${1:-}" = "run" ]; then
  cat >/dev/null
  echo "ollama $2 fake reviewer"
  exit 0
fi
echo "ollama fake reviewer"
""",
        )
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["XDG_CACHE_HOME"] = str(self.cache)
        # Keep git from finding a repository above the temp dir, so "outside git" is real.
        self.env["GIT_CEILING_DIRECTORIES"] = os.path.realpath(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_command(self, command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=str(cwd or REPO_ROOT),
            env=self.env,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def make_git_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        self.run_command(["git", "init"], repo)
        (repo / "example.txt").write_text("hello\n")
        return repo

    def commit_all(self, repo: Path) -> None:
        self.run_command(["git", "add", "-A"], repo)
        commit = self.run_command(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
             "commit", "-q", "-m", "init"],
            repo,
        )
        self.assertEqual(commit.returncode, 0, commit.stdout)

    def codex_command(self, root: Path) -> "run_review.ReviewCommand":
        write_fake_cli(self.bin, "codex", FAKE_CODEX_BODY)
        args = argparse.Namespace(
            commit=None,
            path=None,
            base=None,
            prompt_file=root / ".ai-friend-review" / "prompts" / "2026-10-08-plan.txt",
        )
        command = run_review.build_command({"name": "codex", "path": str(self.bin / "codex")}, args, root)
        assert command
        return command

    def test_doc_review_runs_outside_git_on_the_full_document(self) -> None:
        write_fake_cli(self.bin, "codex", FAKE_CODEX_BODY)
        plans = self.root / "plans"
        plans.mkdir()
        (plans / "plan.md").write_text(
            "# Plan: billing export\n"
            "1. Add an export button to the billing page.\n"
            "2. Call the billing API from the button handler.\n"
            "3. Ship the billing API. SENTINEL-LAST-LINE\n"
        )
        json_out = self.root / "result.json"
        result = self.run_command(
            [sys.executable, str(RUN_REVIEW), "--doc", "plan.md", "--reviewers", "codex",
             "--json-out", str(json_out), "--refresh"],
            plans,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        prompt_line = next(line for line in result.stdout.splitlines() if line.startswith("Full review prompt file: "))
        prompt = Path(prompt_line.split(": ", 1)[1]).read_text()
        self.assertIn("SENTINEL-LAST-LINE", prompt, "reviewers must see the whole document, not a diff")
        self.assertNotIn("changed line", prompt, "a document has no changed lines to anchor findings to")
        [reviewer] = json.loads(json_out.read_text())["reviewers"]
        self.assertEqual(reviewer["status"], "OK", reviewer)
        self.assertEqual([finding["location"] for finding in reviewer["findings"]], ["plan.md:3"])

    def test_doc_rejects_files_it_cannot_review(self) -> None:
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "empty.md").write_text("  \n")
        (docs / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        # Reviewers handed a cut-off document would be credited with text they never saw.
        (docs / "huge.md").write_text("".join(f"Step {n}: a sentence of plan text.\n" for n in range(3000)))
        for target, expected in [
            ("missing.md", "does not exist"),
            (".", "single file"),
            ("", "single file"),
            ("empty.md", "is empty"),
            ("blob.bin", "binary"),
            ("huge.md", "too large"),
        ]:
            with self.subTest(target=target):
                result = self.run_command(
                    [sys.executable, str(RUN_REVIEW), "--dry-run", "--doc", target, "--reviewers", "agy", "--refresh"],
                    docs,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected, result.stdout)

    def test_path_with_nothing_to_review_fails_and_points_to_doc(self) -> None:
        repo = self.make_git_repo()
        self.commit_all(repo)
        unchanged = self.run_command(
            [sys.executable, str(RUN_REVIEW), "--dry-run", "--path", "example.txt", "--reviewers", "agy", "--refresh"],
            repo,
        )
        self.assertNotEqual(unchanged.returncode, 0, unchanged.stdout)
        self.assertIn("no uncommitted changes", unchanged.stdout)
        self.assertIn("--doc", unchanged.stdout)

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "plan.md").write_text("# Plan\n")
        no_repo = self.run_command(
            [sys.executable, str(RUN_REVIEW), "--dry-run", "--path", "plan.md", "--reviewers", "agy", "--refresh"],
            outside,
        )
        self.assertNotEqual(no_repo.returncode, 0, no_repo.stdout)
        self.assertIn("--doc", no_repo.stdout)

    def test_clean_tree_targets_fail_instead_of_reviewing_nothing(self) -> None:
        repo = self.make_git_repo()
        self.commit_all(repo)
        self.run_command(["git", "branch", "same-as-head"], repo)
        for target in (["--uncommitted"], ["--base", "same-as-head"]):
            with self.subTest(target=target[0]):
                result = self.run_command(
                    [sys.executable, str(RUN_REVIEW), "--dry-run", *target, "--reviewers", "agy", "--refresh"],
                    repo,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("nothing to review", result.stdout)

    def test_doc_review_in_a_repo_records_no_commit_sha(self) -> None:
        # The repo's HEAD says nothing about a document's contents, so a gate keyed on
        # SHA must not treat an edited plan as already reviewed.
        repo = self.make_git_repo()
        self.commit_all(repo)
        args = argparse.Namespace(doc="PLAN.md", commit=None, base=None, path=None)
        self.assertEqual(run_review.resolve_shas(args, repo), (None, None))

    def test_codex_adapter_does_not_wait_on_an_open_stdin(self) -> None:
        """Observed: codex 0.160 appends piped stdin to its prompt and waits for EOF.

        Under a harness that never closes a tool's stdin, a review hung for over ten
        minutes printing "Reading additional input from stdin...".
        """
        repo = self.make_git_repo()
        command = self.codex_command(repo)
        with stdin_held_open():
            exit_code, output = run_review.run_reviewer(command, repo, timeout=10)
        self.assertEqual(exit_code, 0, output)

    def test_probes_do_not_inherit_stdin(self) -> None:
        probe = self.bin / "pipe-sensitive"
        probe.write_text(
            "#!/usr/bin/env sh\n"
            "if [ -p /dev/stdin ]; then echo 'stdin is an open pipe' >&2; exit 1; fi\n"
            "echo 'probe 1.0.0'\n"
        )
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
        with stdin_held_open():
            statuses = run_review.preflight([run_review.ReviewCommand(name="probe", command=[str(probe)])], self.root)
            version = discover_agents.version_for(str(probe), ["--version"])
        self.assertEqual(statuses, {"probe": run_review.STATUS_OK})
        self.assertEqual(version, "probe 1.0.0")

    def test_codex_findings_come_from_the_last_message_file(self) -> None:
        repo = self.make_git_repo()
        command = self.codex_command(repo)
        outcome = run_review.run_reviewer_with_retries(command, repo, 10, sleeper=lambda _: None)
        self.assertEqual(outcome.status, run_review.STATUS_OK, outcome.output)
        # codex prints its final message twice on stdout; scraping it doubles every finding.
        self.assertEqual(len(run_review.parse_findings("codex", outcome.output)), 1, outcome.output)
        last_message = Path(command.command[command.command.index("--output-last-message") + 1])
        self.assertTrue(last_message.is_file())
        self.assertTrue(last_message.resolve().is_relative_to((repo / ".ai-friend-review").resolve()))

    def test_command_display_masks_prompts_but_shows_long_paths(self) -> None:
        long_path = "/tmp/" + "nested-run-directory/" * 10 + "2026-10-08-doc-plan-md-codex.md"
        prompt = "Read the complete AI Friend Review prompt from this local file and follow it exactly: " + "x" * 120
        shown = run_review.command_display(["codex", "exec", "--output-last-message", long_path, prompt])
        self.assertIn(long_path, shown)
        self.assertNotIn(prompt, shown)

    def test_stale_last_message_is_never_reused(self) -> None:
        repo = self.make_git_repo()
        command = self.codex_command(repo)
        last_message = Path(command.command[command.command.index("--output-last-message") + 1])
        last_message.parent.mkdir(parents=True, exist_ok=True)
        last_message.write_text("### Finding: stale answer from an earlier attempt\n- **Severity**: P0\n")
        # A codex run that exits cleanly without writing the file must not resurrect it.
        write_fake_cli(self.bin, "codex", 'echo "fresh review from stdout"')
        exit_code, output = run_review.run_reviewer(command, repo, timeout=10)
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output, "fresh review from stdout")

    def test_discovery_finds_all_supported_reviewers(self) -> None:
        result = self.run_command([sys.executable, str(DISCOVER), "--refresh", "--json"])
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        names = [agent["name"] for agent in data["agents"]]
        self.assertEqual(
            names,
            [
                "agy",
                "codex",
                "devin",
                "claude",
                "opencode",
                "cursor",
                "greptile",
                "kiro",
                "gemma3",
                "qwen3",
                "llama3",
            ],
        )

    def test_discovery_no_agents_is_valid_state(self) -> None:
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        self.env["PATH"] = str(empty_bin)
        result = self.run_command([sys.executable, str(DISCOVER), "--refresh", "--json"])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["agents"], [])

        text_result = self.run_command([sys.executable, str(DISCOVER), "--refresh"])
        self.assertEqual(text_result.returncode, 0, text_result.stdout)
        self.assertIn("No supported AI coding agent CLIs found", text_result.stdout)

    def test_ollama_model_fallback_resolution(self) -> None:
        env_name = "AI_FRIEND_OLLAMA_GEMMA3_MODEL"
        original = os.environ.pop(env_name, None)
        try:
            self.assertEqual(discover_agents.resolve_ollama_model("gemma3", {"gemma3:4b"}), "gemma3:4b")
            self.assertIsNone(discover_agents.resolve_ollama_model("gemma3", {"qwen3:0.6b"}))
        finally:
            if original is not None:
                os.environ[env_name] = original

    def test_ranked_dry_run_prefers_external_reviewers(self) -> None:
        repo = self.make_git_repo()
        self.env["AI_FRIEND_REVIEWER_RANKING"] = "Claude,Agy,Claude,OpenCode"
        result = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--current-agent",
                "Codex",
                "--count",
                "3",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertLess(result.stdout.index("- claude:"), result.stdout.index("- agy:"))
        self.assertIn("- agy:", result.stdout)
        self.assertIn("- claude:", result.stdout)
        self.assertIn("- opencode:", result.stdout)
        self.assertNotIn("- devin:", result.stdout)
        self.assertNotIn("- codex:", result.stdout)

    def test_explicit_reviewers_and_include_self(self) -> None:
        repo = self.make_git_repo()
        explicit = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--current-agent",
                "codex",
                "--reviewers",
                "agy,opencode",
                "--count",
                "2",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(explicit.returncode, 0, explicit.stdout)
        self.assertIn("- agy:", explicit.stdout)
        self.assertIn("- opencode:", explicit.stdout)
        self.assertNotIn("- claude:", explicit.stdout)
        self.assertNotIn("- codex:", explicit.stdout)

        include_self = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--current-agent",
                "codex",
                "--count",
                "5",
                "--include-self",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(include_self.returncode, 0, include_self.stdout)
        self.assertIn("- codex:", include_self.stdout)
        self.assertIn(" exec --sandbox read-only ", include_self.stdout)

    def test_new_reviewers_build_expected_dry_run_commands(self) -> None:
        repo = self.make_git_repo()
        result = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--reviewers",
                "cursor,kiro,devin,gemma3,qwen3,llama3",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("- cursor:", result.stdout)
        self.assertIn(" agent --print --mode plan --sandbox enabled ", result.stdout)
        self.assertIn("- kiro:", result.stdout)
        self.assertIn(" chat --no-interactive --trust-tools=fs_read --wrap never ", result.stdout)
        self.assertIn("- devin:", result.stdout)
        self.assertIn(" -p --permission-mode auto --sandbox --prompt-file ", result.stdout)
        self.assertIn("- gemma3:", result.stdout)
        self.assertIn("ollama run gemma3:1b --nowordwrap", result.stdout)
        self.assertIn("- qwen3:", result.stdout)
        self.assertIn("ollama run qwen3:0.6b --nowordwrap --think=false --hidethinking", result.stdout)
        self.assertIn("- llama3:", result.stdout)
        self.assertIn("review prompt sent over stdin", result.stdout)

    def test_ollama_reviewer_uses_generated_review_prompt(self) -> None:
        args = argparse.Namespace(
            commit=None,
            path=None,
            base=None,
            prompt_file=Path("/tmp/prompt.txt"),
            review_prompt="standardized prompt text",
        )
        command = run_review.build_command(
            {"name": "gemma3", "path": "ollama", "model": "gemma3:1b"},
            args,
            REPO_ROOT,
        )
        self.assertIsNotNone(command)
        assert command
        self.assertEqual(command.stdin, "standardized prompt text")

    def test_greptile_requires_base_target(self) -> None:
        repo = self.make_git_repo()
        uncommitted_review = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--reviewers",
                "greptile",
                "--refresh",
            ],
            repo,
        )
        self.assertNotEqual(uncommitted_review.returncode, 0)
        self.assertIn("Reviewer(s) cannot run for this target: greptile (supports --base only)", uncommitted_review.stdout)

        args = argparse.Namespace(
            commit=None,
            path=None,
            base="main",
            prompt_file=Path("/tmp/prompt.txt"),
        )
        command = run_review.build_command({"name": "greptile", "path": "greptile"}, args, REPO_ROOT)
        self.assertIsNotNone(command)
        assert command
        self.assertEqual(command.command, ["greptile", "review", "--agent", "--no-color", "--branch", "main"])

        path_review = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--path",
                "example.txt",
                "--reviewers",
                "greptile",
                "--refresh",
            ],
            repo,
        )
        self.assertNotEqual(path_review.returncode, 0)
        self.assertIn("Reviewer(s) cannot run for this target: greptile (supports --base only)", path_review.stdout)

    def test_auto_ranked_greptile_skips_for_uncommitted_target(self) -> None:
        repo = self.make_git_repo()
        self.env["AI_FRIEND_REVIEWER_RANKING"] = "greptile,agy"
        result = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--uncommitted",
                "--count",
                "2",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Skipped reviewer(s): greptile (supports --base only)", result.stdout)
        self.assertIn("- agy:", result.stdout)
        self.assertNotIn("- greptile:", result.stdout)

    def test_greptile_rejected_for_commit_target(self) -> None:
        args = argparse.Namespace(
            commit="abc1234",
            path=None,
            base=None,
            prompt_file=Path("/tmp/prompt.txt"),
        )
        command = run_review.build_command({"name": "greptile", "path": "greptile"}, args, REPO_ROOT)
        self.assertIsNone(command)

    def test_aggregation_clusters_structured_findings(self) -> None:
        outputs = {
            "agy": """### Finding: Missing validation
- **Severity**: P1
- **Location**: skills/foo.py:42
- **Evidence**: Input is used without validation.
- **Confidence**: High
- **Why it matters**: Bad data can pass through.
- **Suggested fix**: Validate before use.
""",
            "opencode": """### Finding: Unvalidated input
- **Severity**: P2
- **Location**: skills/foo.py:L42-L45
- **Evidence**: The same input reaches the sink.
- **Confidence**: Medium
- **Why it matters**: Runtime failure.
- **Suggested fix**: Add a guard.

### Finding: Possible docs drift
- **Severity**: P3
- **Location**: README.md
- **Evidence**: The command name may be stale.
- **Confidence**: Low
- **Why it matters**: Confusing docs.
- **Suggested fix**: Verify the command.
""",
        }
        clusters = run_review.cluster_findings(outputs)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(run_review.agreement_label(clusters[0]), "Confirmed by multiple reviewers")
        self.assertEqual({finding.reviewer for finding in clusters[0].findings}, {"agy", "opencode"})
        self.assertEqual(run_review.agreement_label(clusters[1]), "Likely false positive or needs verification")

    def test_real_run_writes_aggregated_findings(self) -> None:
        write_fake_cli(
            self.bin,
            "agy",
            """cat <<'FINDINGS'
### Finding: Missing validation
- **Severity**: P1
- **Location**: app.py:12
- **Evidence**: The request body is used without validation.
- **Confidence**: High
- **Why it matters**: Bad input reaches core behavior.
- **Suggested fix**: Validate before use.
FINDINGS""",
        )
        write_fake_cli(
            self.bin,
            "opencode",
            """cat <<'FINDINGS'
### Finding: Unvalidated input
- **Severity**: P2
- **Location**: app.py:L12-L14
- **Evidence**: The handler passes unchecked data onward.
- **Confidence**: Medium
- **Why it matters**: Invalid input can crash the flow.
- **Suggested fix**: Add a guard.
FINDINGS""",
        )
        repo = self.make_git_repo()
        result = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--uncommitted",
                "--reviewers",
                "agy,opencode",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        report_line = next(line for line in result.stdout.splitlines() if line.startswith("Report written: "))
        report_path = Path(re.sub(r"^Report written: ", "", report_line))
        report = report_path.read_text()
        self.assertIn("## Aggregated Findings", report)
        self.assertIn("Confirmed by multiple reviewers", report)
        self.assertIn("Reviewers: `agy, opencode`", report)
        self.assertIn("app.py:12", report)

    def test_binary_context_and_high_signal_are_bounded(self) -> None:
        repo = self.make_git_repo()
        binary = repo / "image.bin"
        binary.write_bytes(b"\x00\x01\x02not text")
        utf8 = repo / "notes.md"
        utf8.write_text("café résumé こんにちは\n")
        context = run_review.untracked_file_context(repo, ["image.bin"])
        self.assertIn("[skipped binary-looking file]", context)
        self.assertNotIn("\x00", context)
        utf8_context = run_review.untracked_file_context(repo, ["notes.md"])
        self.assertIn("café résumé", utf8_context)

        noisy = {
            "agy": """No P0 issues found.
### Finding: Real issue
- **Severity**: P2
- **Location**: app.py:3
- **Evidence**: observed
- **Confidence**: High
- **Why it matters**: impact
- **Suggested fix**: fix
"""
        }
        self.assertEqual(run_review.extract_high_signal(noisy), ["agy: P2 app.py:3 Real issue"])

    def test_missing_path_fails_clearly(self) -> None:
        repo = self.make_git_repo()
        result = self.run_command(
            [
                sys.executable,
                str(RUN_REVIEW),
                "--dry-run",
                "--path",
                "missing.py",
                "--reviewers",
                "agy",
                "--refresh",
            ],
            repo,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--path does not exist or match a tracked file", result.stdout)


class FailureClassification(unittest.TestCase):
    """A reviewer that did not run must never be indistinguishable from one that approved.

    Every string below was observed in a real stored review under .ai-friend-review/reviews/.
    """

    def test_quota_messages_classify_as_quota(self) -> None:
        for output in [
            "Error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 63h51m48s.",
            "ActionRequiredError: You've hit your usage limit Get Cursor Pro for more Agent usage",
            "You've hit your session limit · resets 10:30pm (America/Phoenix)",
        ]:
            with self.subTest(output=output[:40]):
                self.assertEqual(run_review.classify_outcome(1, output), run_review.STATUS_QUOTA)

    def test_other_failures_classify_distinctly(self) -> None:
        cases = [
            (124, "Reviewer timed out after 900s.", run_review.STATUS_TIMEOUT),
            (127, "Reviewer launch failed: [Errno 2] No such file or directory", run_review.STATUS_NOT_INSTALLED),
            (2, "error: the argument '--uncommitted' cannot be used with '[PROMPT]'", run_review.STATUS_ADAPTER_ERROR),
            (1, "error: there are no committed code changes to review against main", run_review.STATUS_UNSUPPORTED_TARGET),
            (0, "", run_review.STATUS_EMPTY_OUTPUT),
            (0, "### Finding: x\n- **Severity**: P1", run_review.STATUS_OK),
        ]
        for code, output, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(run_review.classify_outcome(code, output), expected)

    def test_successful_exit_is_never_reclassified_by_review_content(self) -> None:
        """A reviewer's output contains its REVIEW, which quotes arbitrary prose.

        Observed: codex exited 0 with a full review mentioning "no such file or
        directory" and was reported as NOT_INSTALLED, discarding its findings.
        """
        review = (
            "### Finding: missing config\n- **Severity**: P1\n"
            "- **Evidence**: open() raised No such file or directory\n"
        )
        self.assertEqual(run_review.classify_outcome(0, review), run_review.STATUS_OK)
        for phrase in ["command not found", "no such file or directory", "usage: foo"]:
            with self.subTest(phrase=phrase):
                body = f"### Finding: x\n- **Severity**: P2\n- **Evidence**: {phrase}\n"
                self.assertEqual(run_review.classify_outcome(0, body), run_review.STATUS_OK)

    def test_soft_quota_failure_on_exit_zero_is_still_caught(self) -> None:
        # No findings + a quota message = the CLI bailed without reviewing.
        self.assertEqual(
            run_review.classify_outcome(0, "Error: Individual quota reached. Resets in 2h."),
            run_review.STATUS_QUOTA,
        )
        # But a real review that merely mentions a rate limit stays OK.
        self.assertEqual(
            run_review.classify_outcome(
                0, "### Finding: retry storm\n- **Severity**: P1\n- **Evidence**: 429 rate limit\n"
            ),
            run_review.STATUS_OK,
        )

    def test_template_echo_is_empty_output(self) -> None:
        """Observed: codex returned only the prompt's format block and the run reported OK."""
        self.assertEqual(run_review.classify_outcome(0, TEMPLATE_ECHO), run_review.STATUS_EMPTY_OUTPUT)
        self.assertEqual(run_review.extract_high_signal({"codex": TEMPLATE_ECHO}), [])
        self.assertEqual(run_review.cluster_findings({"codex": TEMPLATE_ECHO}), [])

    def test_template_echo_beside_a_real_finding_keeps_only_the_real_one(self) -> None:
        output = TEMPLATE_ECHO + (
            "\n### Finding: Real issue\n- **Severity**: P2\n- **Location**: app.py:3\n"
            "- **Evidence**: observed\n- **Confidence**: High\n"
        )
        self.assertEqual(run_review.classify_outcome(0, output), run_review.STATUS_OK)
        self.assertEqual(run_review.extract_high_signal({"codex": output}), ["codex: P2 app.py:3 Real issue"])

    def test_finding_that_quotes_a_placeholder_is_still_a_finding(self) -> None:
        # Reviewing this repo's own prompt code will quote the template in evidence.
        output = (
            "### Finding: Prompt template leaks into the report\n- **Severity**: P2\n"
            "- **Location**: run_review.py:464\n- **Evidence**: `<short title>` is printed verbatim.\n"
        )
        self.assertEqual(run_review.classify_outcome(0, output), run_review.STATUS_OK)
        self.assertEqual(len(run_review.parse_findings("codex", output)), 1)

    def test_one_angle_bracket_field_does_not_make_a_finding_an_echo(self) -> None:
        for title, location in [
            ("`<Suspense>`", "app/page.tsx:12"),
            ("Unchecked input reaches the parser", "<unknown>"),
            ("Prompt read from stdin is never closed", "`<stdin>`"),
        ]:
            with self.subTest(title=title, location=location):
                output = (
                    f"### Finding: {title}\n- **Severity**: P2\n- **Location**: {location}\n"
                    "- **Evidence**: observed in the diff\n- **Confidence**: Medium\n"
                    "- **Why it matters**: a real defect\n- **Suggested fix**: guard it\n"
                )
                self.assertEqual(run_review.classify_outcome(0, output), run_review.STATUS_OK)
                self.assertEqual(len(run_review.parse_findings("codex", output)), 1)

    def test_doc_template_echo_is_empty_output(self) -> None:
        # The doc prompt fills the file name into the location, so only part of it is a placeholder.
        echo = """### Finding: <short title>
- **Severity**: P0 | P1 | P2 | P3
- **Location**: plan.md:<line or range>
- **Evidence**: <the document text at that location, plus any file or command you checked>
- **Confidence**: Low | Medium | High
- **Why it matters**: <impact>
- **Suggested fix**: <specific change to the document, or the check that would settle it>
"""
        self.assertEqual(run_review.classify_outcome(0, echo), run_review.STATUS_EMPTY_OUTPUT)

    def test_quota_reset_gates_retry(self) -> None:
        # A long lockout must not be retried; a short one may be.
        _, long_wait = run_review.parse_quota_reset("Resets in 63h51m48s.")
        self.assertGreater(long_wait, run_review.QUOTA_RETRY_MAX_WAIT_SECONDS)
        _, short_wait = run_review.parse_quota_reset("resets in 45s")
        self.assertLessEqual(short_wait, run_review.QUOTA_RETRY_MAX_WAIT_SECONDS)
        # A clock time carries no date or timezone; guessing one would produce a
        # confidently wrong retry decision, so it is treated as a long wait.
        _, clock_wait = run_review.parse_quota_reset("resets 10:30pm (America/Phoenix)")
        self.assertEqual(clock_wait, float("inf"))
        self.assertEqual(run_review.parse_quota_reset("nothing here"), (None, None))

    def test_quota_is_not_retried_but_timeout_is(self) -> None:
        calls = {"n": 0}

        def fake_run(command, root, timeout):  # noqa: ARG001
            calls["n"] += 1
            return 1, "Individual quota reached. Resets in 63h51m48s."

        original = run_review.run_reviewer
        run_review.run_reviewer = fake_run
        try:
            outcome = run_review.run_reviewer_with_retries(
                run_review.ReviewCommand(name="agy", command=["agy"]), Path("."), 900, sleeper=lambda _: None
            )
        finally:
            run_review.run_reviewer = original
        self.assertEqual(outcome.status, run_review.STATUS_QUOTA)
        self.assertEqual(calls["n"], 1, "a long quota lockout must not be retried")
        self.assertFalse(outcome.ok)
        self.assertIn("quota lockout", outcome.retries_skipped_reason)

        calls["n"] = 0

        def fake_timeout(command, root, timeout):  # noqa: ARG001
            calls["n"] += 1
            return 124, "Reviewer timed out after 900s."

        run_review.run_reviewer = fake_timeout
        try:
            outcome = run_review.run_reviewer_with_retries(
                run_review.ReviewCommand(name="devin", command=["devin"]), Path("."), 900, sleeper=lambda _: None
            )
        finally:
            run_review.run_reviewer = original
        self.assertEqual(outcome.status, run_review.STATUS_TIMEOUT)
        self.assertEqual(calls["n"], 3, "a timeout gets the original attempt plus two retries")

    def test_opt_in_reviewers_are_not_auto_selected(self) -> None:
        agents = [{"name": name} for name in ["cursor", "agy", "greptile", "opencode", "codex"]]
        selected = run_review.select_reviewers(agents, [], None, False, None)
        names = {agent["name"] for agent in selected}
        self.assertFalse(names & run_review.OPT_IN_REVIEWERS, f"opt-in reviewer auto-selected: {names}")

    def test_opt_in_reviewers_are_reachable_when_requested(self) -> None:
        agents = [{"name": name} for name in ["cursor", "opencode"]]
        selected = run_review.select_reviewers(agents, ["cursor"], None, False, None)
        self.assertEqual([agent["name"] for agent in selected], ["cursor"])

    def test_timeout_floor_is_enforced(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUN_REVIEW), "--uncommitted", "--timeout", "15"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--timeout must be at least", result.stdout)


if __name__ == "__main__":
    unittest.main()
