#!/usr/bin/env bash
set -euo pipefail

# Contributor installer: symlink this repo's skill into local agent loaders.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_NAME="ai-friend-review"
SKILL_SRC="$REPO_DIR/skills/$SKILL_NAME"
SHARED_SKILLS_DIR="${AI_FRIEND_SHARED_SKILLS_DIR:-$HOME/.agents/skills}"
SHARED_LINK="$SHARED_SKILLS_DIR/$SKILL_NAME"

link_one() {
  local src="$1"
  local dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [ -L "$dest" ]; then
    rm "$dest"
  elif [ -e "$dest" ]; then
    if [ "${AI_FRIEND_REPLACE_EXISTING:-0}" = "1" ]; then
      rm -rf "$dest"
    else
      echo "error: $dest exists and is not a symlink" >&2
      echo "Set AI_FRIEND_REPLACE_EXISTING=1 to replace a generated install copy." >&2
      exit 1
    fi
  fi
  ln -s "$src" "$dest"
  echo "linked $dest -> $src"
}

write_wrapper() {
  local dir="$1"
  local file="$dir/$SKILL_NAME.md"
  mkdir -p "$dir"
  cat > "$file" <<WRAPPER
Use the ai-friend-review skill. If the skill is not loaded automatically, read:

$SHARED_LINK/SKILL.md

Then discover local AI coding agents and run a read-only AI friend review for the user's requested target.
WRAPPER
  echo "wrote wrapper $file"
}

if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
  echo "missing skill source: $SKILL_SRC/SKILL.md" >&2
  exit 1
fi

link_one "$SKILL_SRC" "$SHARED_LINK"
link_one "$SHARED_LINK" "${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/$SKILL_NAME"
link_one "$SHARED_LINK" "${CODEX_SKILLS_DIR:-$HOME/.codex/skills}/$SKILL_NAME"
link_one "$SHARED_LINK" "${ANTIGRAVITY_SKILLS_DIR:-$HOME/.gemini/antigravity-cli/skills}/$SKILL_NAME"

write_wrapper "${OPENCODE_COMMAND_DIR:-$HOME/.config/opencode/command}"
write_wrapper "${OPENCODE_COMMANDS_DIR:-$HOME/.config/opencode/commands}"
write_wrapper "${CURSOR_COMMANDS_DIR:-$HOME/.cursor/commands}"

echo "Done. Restart your coding agent or start a new session to load the skill."
