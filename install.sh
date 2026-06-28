#!/usr/bin/env bash
set -euo pipefail

# One-line installer for the ai-friend-review coding-agent skill.
#
#   curl -fsSL https://raw.githubusercontent.com/kyle-chalmers/ai-friend-review/main/install.sh | bash
#
# Installs into ~/.agents/skills first, then links common agent skill dirs.

REPO_RAW="${AI_FRIEND_REPO_RAW:-https://raw.githubusercontent.com/kyle-chalmers/ai-friend-review/main}"
SKILL_NAME="ai-friend-review"
SHARED_SKILLS_DIR="${AI_FRIEND_SHARED_SKILLS_DIR:-$HOME/.agents/skills}"
SHARED_DEST="$SHARED_SKILLS_DIR/$SKILL_NAME"

download() {
  local src="$1"
  local dest="$2"
  mkdir -p "$(dirname "$dest")"
  curl -fsSL "$REPO_RAW/$src" -o "$dest"
}

install_shared_copy() {
  mkdir -p "$SHARED_DEST/scripts" "$SHARED_DEST/references" "$SHARED_DEST/agents"
  download "skills/$SKILL_NAME/SKILL.md" "$SHARED_DEST/SKILL.md"
  download "skills/$SKILL_NAME/scripts/discover_agents.py" "$SHARED_DEST/scripts/discover_agents.py"
  download "skills/$SKILL_NAME/scripts/run_review.py" "$SHARED_DEST/scripts/run_review.py"
  download "skills/$SKILL_NAME/references/multi-ai-review-research.md" "$SHARED_DEST/references/multi-ai-review-research.md"
  download "skills/$SKILL_NAME/agents/openai.yaml" "$SHARED_DEST/agents/openai.yaml"
  chmod +x "$SHARED_DEST/scripts/discover_agents.py" "$SHARED_DEST/scripts/run_review.py"
  echo "installed $SKILL_NAME -> $SHARED_DEST"
}

link_into() {
  local skills_dir="$1"
  local link="$skills_dir/$SKILL_NAME"
  mkdir -p "$skills_dir"
  if [ -L "$link" ]; then
    rm "$link"
  elif [ -e "$link" ]; then
    echo "skip: $link exists and is not a symlink" >&2
    return
  fi
  ln -s "$SHARED_DEST" "$link"
  echo "linked $link -> $SHARED_DEST"
}

write_wrapper() {
  local dir="$1"
  local file="$dir/$SKILL_NAME.md"
  mkdir -p "$dir"
  cat > "$file" <<'WRAPPER'
Use the ai-friend-review skill. If the skill is not loaded automatically, read:

~/.agents/skills/ai-friend-review/SKILL.md

Then discover local AI coding agents and run a read-only AI friend review for the user's requested target.
WRAPPER
  echo "wrote wrapper $file"
}

install_shared_copy
link_into "${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
link_into "${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
link_into "${ANTIGRAVITY_SKILLS_DIR:-$HOME/.gemini/antigravity-cli/skills}"

write_wrapper "${OPENCODE_COMMAND_DIR:-$HOME/.config/opencode/command}"
write_wrapper "${OPENCODE_COMMANDS_DIR:-$HOME/.config/opencode/commands}"
write_wrapper "${CURSOR_COMMANDS_DIR:-$HOME/.cursor/commands}"

echo "Done. Start a new agent session, then invoke AI Friend Review by name."
