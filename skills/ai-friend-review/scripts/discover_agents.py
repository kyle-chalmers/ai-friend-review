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
        "review_template": [],
        "notes": "Detected for inventory only. No stable headless review template is used.",
        "headless_review": False,
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


def discover() -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    for name, meta in AGENTS.items():
        path = command_v(name)
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
            }
        )

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

    merged = []
    for name in AGENTS:
        if name in current_by_name:
            merged.append(current_by_name[name])
        elif name in cached_by_name:
            old = dict(cached_by_name[name])
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
        version = f" ({agent['version']})" if agent.get("version") else ""
        print(f"- {agent['name']}: {agent['path']}{version} [{status}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
