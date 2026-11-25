#!/usr/bin/env bash
set -euo pipefail

EXCHANGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$EXCHANGE_ROOT/00_全局中枢_Hub"
CFG_DIR="$HOME/.config/research_hub"

mkdir -p "$EXCHANGE_ROOT/01_在研课题_Projects" \
         "$EXCHANGE_ROOT/02_公共资料库_Library" \
         "$EXCHANGE_ROOT/99_历史归档_Archive" \
         "$CFG_DIR"

if [[ ! -f "$CFG_DIR/.env" ]]; then
  cp "$EXCHANGE_ROOT/.env.example" "$CFG_DIR/.env"
  chmod 600 "$CFG_DIR/.env"
  echo "[ResearchHub] Created $CFG_DIR/.env (please fill in your API keys)."
else
  echo "[ResearchHub] Existing $CFG_DIR/.env preserved."
fi

cat <<EOF

[ResearchHub] Initialization complete!
Add the following block to your ~/.zshrc (or ~/.bashrc), then run 'source ~/.zshrc':

# === ResearchHub CLI Aliases ===
export RESEARCH_HUB_ROOT="$EXCHANGE_ROOT"
alias consult='python3 "$HUB_DIR/consult.py"'
alias checkms='python3 "$HUB_DIR/scripts/check_manuscript.py"'
alias newproj='python3 "$HUB_DIR/new_project.py"'
alias hub='cd "$HUB_DIR"'
alias hubhelp='python3 "$HUB_DIR/scripts/hub_help.py"'
alias ppt='python3 "$HUB_DIR/scripts/ppt_studio.py"'
alias runnotify='python3 "$HUB_DIR/scripts/notify_when_done.py" run'
alias watchdone='python3 "$HUB_DIR/scripts/notify_when_done.py" watch'
alias codexbox='python3 "$HUB_DIR/scripts/codex_sandbox.py"'
alias dash='python3 "$HUB_DIR/dashboard/serve.py" --port 8765'
mdview() { glow -p "\${1:-_bus/reply.md}"; }
[ -f "\$HOME/.config/research_hub/projects.zsh" ] && source "\$HOME/.config/research_hub/projects.zsh"
EOF
