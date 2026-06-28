#!/usr/bin/env python3
"""Unit-style checks for AI Friend Review helper scripts."""

from __future__ import annotations

import json
import importlib.util
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


class ScriptChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.cache = self.root / "cache"
        self.bin.mkdir()
        for name in ["agy", "codex", "claude", "opencode", "cursor", "gemini"]:
            write_fake_cli(self.bin, name)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["XDG_CACHE_HOME"] = str(self.cache)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_command(self, command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=str(cwd or REPO_ROOT),
            env=self.env,
            check=False,
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

    def test_discovery_ignores_gemini_cli_reviewer(self) -> None:
        result = self.run_command([sys.executable, str(DISCOVER), "--refresh", "--json"])
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        names = [agent["name"] for agent in data["agents"]]
        self.assertEqual(names, ["agy", "codex", "claude", "opencode", "cursor"])

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
                "4",
                "--include-self",
                "--refresh",
            ],
            repo,
        )
        self.assertEqual(include_self.returncode, 0, include_self.stdout)
        self.assertIn("- codex:", include_self.stdout)
        self.assertIn(" exec --sandbox read-only ", include_self.stdout)

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


if __name__ == "__main__":
    unittest.main()
