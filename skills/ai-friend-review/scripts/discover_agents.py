#!/usr/bin/env python3
"""Discover local AI coding agent CLIs and cache safe invocation metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AGENTS: dict[str, dict[str, Any]] = {
    "agy": {
        "version_args": ["--version"],
        "review_template": ["agy", "--print", "<prompt-file-instruction>", "--sandbox"],
        "notes": "Antigravity CLI. Use print mode with a prompt-file instruction and sandboxing for read-only review.",
        "headless_review": True,
    },
    "codex": {
        "version_args": ["--version"],
        "review_template": ["codex", "exec", "--sandbox", "read-only", "<prompt-file-instruction>"],
        "notes": "Use read-only exec mode with a prompt-file instruction for standardized review.",
        "headless_review": True,
    },
    "devin": {
        "version_args": ["--version"],
        "review_template": ["devin", "-p", "--permission-mode", "auto", "--sandbox", "--prompt-file", "<prompt-file>"],
        "notes": "Use print mode with auto read-only permissions, OS sandboxing, and the shared prompt file.",
        "headless_review": True,
    },
    "claude": {
        "version_args": ["--version"],
        "review_template": ["claude", "-p", "--permission-mode", "plan", "<prompt-file-instruction>"],
        "notes": "Use print mode with plan permissions and a prompt-file instruction for read-only review.",
        "headless_review": True,
    },
    "opencode": {
        "version_args": ["--version"],
        "review_template": ["opencode", "run", "--agent", "plan", "--dir", "<repo>", "<prompt-file-instruction>"],
        "notes": "Use run mode with the plan agent and a prompt-file instruction when available.",
        "headless_review": True,
    },
    "cursor": {
        "version_args": ["--version"],
        "review_template": [
            "cursor",
            "agent",
            "--print",
            "--mode",
            "plan",
            "--sandbox",
            "enabled",
            "--trust",
            "--workspace",
            "<repo>",
            "<prompt-file-instruction>",
        ],
        "notes": "Use Cursor Agent print mode with plan mode, sandboxing, and a prompt-file instruction.",
        "headless_review": True,
    },
    "greptile": {
        "version_args": ["--version"],
        "review_template": ["greptile", "review", "--agent", "--no-color", "--branch", "<base>"],
        "notes": "Native branch review adapter. Greptile reviews committed branch diffs against a base branch instead of reading the shared prompt file.",
        "headless_review": True,
    },
    "kiro": {
        "binary": "kiro-cli-chat",
        "version_args": ["--version"],
        "review_template": [
            "kiro-cli-chat",
            "chat",
            "--no-interactive",
            "--trust-tools=fs_read",
            "--wrap",
            "never",
            "<prompt-file-instruction>",
        ],
        "notes": "Use non-interactive chat with only fs_read trusted so Kiro can read the prompt file without write or command permissions.",
        "headless_review": True,
    },
}

OLLAMA_REVIEWERS: dict[str, dict[str, str]] = {
    "gemma3": {
        "model": "gemma3:1b",
        "env": "AI_FRIEND_OLLAMA_GEMMA3_MODEL",
        "notes": "Local Ollama Gemma 3 reviewer. The full standardized prompt is sent over stdin.",
    },
    "qwen3": {
        "model": "qwen3:0.6b",
        "env": "AI_FRIEND_OLLAMA_QWEN3_MODEL",
        "notes": "Local Ollama Qwen 3 reviewer. The full standardized prompt is sent over stdin.",
    },
    "llama3": {
        "model": "llama3:8b-instruct-q2_K",
        "env": "AI_FRIEND_OLLAMA_LLAMA3_MODEL",
        "notes": "Local Ollama Llama 3 reviewer. The full standardized prompt is sent over stdin.",
    },
}


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    if root:
        return Path(root).expanduser() / "ai-friend-review"
    return Path.home() / ".cache" / "ai-friend-review"


def cache_path() -> Path:
    return cache_dir() / "agents.json"


def command_v(name: str) -> str | None:
    """Use command -v semantics without reading shell history or config files."""
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def version_for(path: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [path, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    lines.extend(line.strip() for line in result.stderr.splitlines() if line.strip())
    versionish = re.compile(r"\b\d+(?:\.\d+)+(?:[-+][A-Za-z0-9_.-]+)?\b|\bv?\d+\b")
    fallback: str | None = None
    for line in lines:
        lowered = line.lower()
        if lowered.startswith("warning:") or lowered.startswith("warn:"):
            continue
        if fallback is None:
            fallback = line
        if versionish.search(line):
            return line
    return fallback


def ollama_models(path: str) -> set[str]:
    try:
        result = subprocess.run(
            [path, "list"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()

    if result.returncode != 0:
        return set()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if name.upper() == "NAME" or ":" not in name:
            continue
        names.add(name)
    return names


def ollama_run_help(path: str) -> str:
    try:
        result = subprocess.run(
            [path, "run", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(part for part in [result.stdout, result.stderr] if part)


def resolve_ollama_model(name: str, installed_models: set[str]) -> str | None:
    meta = OLLAMA_REVIEWERS[name]
    configured = os.environ.get(meta["env"])
    if configured:
        return configured if configured in installed_models else None

    default = meta["model"]
    if default in installed_models:
        return default

    prefix = default.split(":", 1)[0] + ":"
    matches = sorted(model for model in installed_models if model.startswith(prefix))
    return matches[0] if matches else None


def discover() -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    for name, meta in AGENTS.items():
        binary = meta.get("binary", name)
        path = command_v(binary)
        if not path:
            continue
        agents.append(
            {
                "name": name,
                "path": path,
                "version": version_for(path, meta["version_args"]),
                "headless_review": bool(meta["headless_review"]),
                "review_template": meta["review_template"],
                "notes": meta["notes"],
                "binary": binary,
            }
        )

    ollama_path = command_v("ollama")
    if ollama_path:
        installed_models = ollama_models(ollama_path)
        ollama_version = version_for(ollama_path, ["--version"])
        run_help = ollama_run_help(ollama_path)
        supports_thinking_flags = "--hidethinking" in run_help and "--think" in run_help
        for name, meta in OLLAMA_REVIEWERS.items():
            model = resolve_ollama_model(name, installed_models)
            if not model:
                continue
            review_template = ["ollama", "run", model, "--nowordwrap"]
            if name == "qwen3" and supports_thinking_flags:
                review_template.extend(["--think=false", "--hidethinking"])
            review_template.append("<stdin-review-prompt>")
            agent = {
                "name": name,
                "path": ollama_path,
                "version": ollama_version,
                "headless_review": True,
                "review_template": review_template,
                "notes": meta["notes"],
                "model": model,
                "provider": "ollama",
            }
            if name == "qwen3":
                agent["supports_thinking_flags"] = supports_thinking_flags
            agents.append(agent)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_file": str(cache_path()),
        "agents": agents,
        "privacy": "Stores executable paths, versions, and invocation templates only.",
    }


def load_cache() -> dict[str, Any] | None:
    path = cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_cache(data: dict[str, Any]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def merge_with_quick_scan(cached: dict[str, Any]) -> dict[str, Any]:
    current = discover()
    cached_by_name = {agent["name"]: agent for agent in cached.get("agents", [])}
    current_by_name = {agent["name"]: agent for agent in current.get("agents", [])}
    ollama_path = command_v("ollama")

    merged = []
    for name in [*AGENTS, *OLLAMA_REVIEWERS]:
        if name in current_by_name:
            merged.append(current_by_name[name])
        elif name in cached_by_name:
            old = dict(cached_by_name[name])
            if name in OLLAMA_REVIEWERS and ollama_path:
                old["missing_model"] = True
                old.pop("missing_from_path", None)
            else:
                old["missing_from_path"] = True
            merged.append(old)

    cached["agents"] = merged
    cached["checked_at"] = datetime.now(timezone.utc).isoformat()
    cached["cache_file"] = str(cache_path())
    return cached


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover local AI coding agents.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cache and rediscover all tools.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text summary.")
    args = parser.parse_args()

    cached = None if args.refresh else load_cache()
    data = discover() if cached is None else merge_with_quick_scan(cached)
    save_cache(data)

    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    print(f"AI Friend Review agent cache: {data['cache_file']}")
    if not data["agents"]:
        print("No supported AI coding agent CLIs found on PATH.")
        return 0

    for agent in data["agents"]:
        status = "reviewer" if agent.get("headless_review") else "inventory only"
        if agent.get("missing_from_path"):
            status = "missing from PATH, cached"
        if agent.get("missing_model"):
            status = "missing Ollama model, cached"
        version = f" ({agent['version']})" if agent.get("version") else ""
        print(f"- {agent['name']}: {agent['path']}{version} [{status}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
