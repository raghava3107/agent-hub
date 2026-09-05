#!/usr/bin/env bash
# ============================================================
# sync-skills.sh
# Keeps Claude skills + commands in sync between:
#   ~/.claude/{skills,commands}     (used by Claude Code CLI)
#   <managed-skills-repo>/claude/{skills,commands}  (git-versioned)
#
# One-time setup:  bin/sync-skills.sh install
# Ad-hoc sync:     bin/sync-skills.sh sync
# See status:      bin/sync-skills.sh status
# ============================================================

set -euo pipefail

# ---------- paths ----------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_HUB="$(cd "$HERE/.." && pwd)"
CONFIG_PATH="${HUB_CONFIG_PATH:-$AGENT_HUB/config/local.json}"

HOME_SKILLS="$HOME/.claude/skills"
HOME_COMMANDS="$HOME/.claude/commands"
MANAGED_REPO=""
if [[ -f "$CONFIG_PATH" ]] && command -v jq >/dev/null 2>&1; then
  MANAGED_REPO="$(jq -r '.skills.managed_repo // empty' "$CONFIG_PATH" 2>/dev/null || true)"
fi
MANAGED_REPO="${HUB_MANAGED_REPO:-$MANAGED_REPO}"
if [[ -z "$MANAGED_REPO" ]]; then
  MANAGED_REPO="$AGENT_HUB"
fi
REPO_SKILLS="${HUB_REPO_SKILLS_DIR:-$MANAGED_REPO/claude/skills}"
REPO_COMMANDS="${HUB_REPO_COMMANDS_DIR:-$MANAGED_REPO/claude/commands}"

PLIST_PREFIX="com.myagents"
if [[ -f "$CONFIG_PATH" ]]; then
  PLIST_PREFIX="$(jq -r '.agent_hub.plist_label_prefix // "com.myagents"' "$CONFIG_PATH" 2>/dev/null || echo "com.myagents")"
fi

PLIST_LABEL="${PLIST_PREFIX}.sync-skills"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_FILE="/tmp/rr-agents-sync.log"

# ---------- helpers ----------
c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_grn()   { printf '\033[32m%s\033[0m' "$*"; }
c_ylw()   { printf '\033[33m%s\033[0m' "$*"; }
c_blu()   { printf '\033[34m%s\033[0m' "$*"; }
c_dim()   { printf '\033[2m%s\033[0m' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m' "$*"; }

info()  { echo "  $*"; }
step()  { echo; echo "$(c_bold "▸ $*")"; }
ok()    { echo "  $(c_grn '✓') $*"; }
warn()  { echo "  $(c_ylw '!') $*"; }
err()   { echo "  $(c_red '✗') $*" >&2; }

ensure_dirs() {
  mkdir -p "$HOME_SKILLS" "$HOME_COMMANDS" "$REPO_SKILLS" "$REPO_COMMANDS"
}

is_symlink_to() {
  # $1 = path, $2 = expected target
  [[ -L "$1" ]] && [[ "$(readlink "$1")" == "$2" ]]
}

# Print resolved absolute path of $1 (handles symlinks)
abspath() {
  python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"
}

# ---------- commands ----------

cmd_help() {
  cat <<EOF
$(c_bold "Spidey — Skill Sync")

$(c_bold "Usage:") bin/sync-skills.sh <command>

$(c_bold "Setup once, sync forever:")
  $(c_grn "install")    One-command setup. Merges home → repo, symlinks home to repo,
              installs a macOS auto-sync watcher. Recommended.

$(c_bold "Manual / diagnostic:")
  $(c_grn "status")     Show which locations are linked, which drift, which conflict.
  $(c_grn "sync")       Bidirectional rsync (newer file wins). Safe to run any time.
  $(c_grn "link")       Merge then symlink home → repo. No daemon.
  $(c_grn "unlink")     Break symlinks and restore real directories (copies from repo).

$(c_bold "Daemon:")
  $(c_grn "install-daemon")    Install just the launchd watcher.
  $(c_grn "uninstall-daemon")  Remove the launchd watcher.

$(c_bold "Paths:")
  home skills:     $HOME_SKILLS
  home commands:   $HOME_COMMANDS
  managed skills:  $REPO_SKILLS
  managed commands:$REPO_COMMANDS
EOF
}

# For each pair (home, repo) print a status line.
# Uses `find -L` so per-file symlinks pointing into repo are counted as files.
_status_pair() {
  local label="$1" home="$2" repo="$3"

  if [[ ! -d "$repo" ]]; then
    echo "  $label: $(c_ylw 'repo dir missing') — $repo"
    return
  fi
  if [[ ! -e "$home" ]]; then
    echo "  $label: $(c_ylw 'home dir missing') — creating on next run"
    return
  fi

  # Case A: whole dir is symlinked to repo — perfect state.
  if [[ -L "$home" ]] && [[ "$(abspath "$home")" == "$(abspath "$repo")" ]]; then
    local n
    n=$(find -L "$repo" -maxdepth 2 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
    echo "  $label: $(c_grn '✓ fully linked') → $repo ($n files)"
    return
  fi

  # Case B: home is a real dir, possibly containing per-file symlinks into repo.
  # Compare filenames; follow symlinks; then hash to detect real drift.
  local h_files r_files h_only r_only common drift symlinked
  h_files=$(cd "$home" && find -L . -type f -name '*.md' 2>/dev/null | sort)
  r_files=$(cd "$repo" && find -L . -type f -name '*.md' 2>/dev/null | sort)
  h_only=$(comm -23 <(echo "$h_files") <(echo "$r_files") | grep -c . || true)
  r_only=$(comm -13 <(echo "$h_files") <(echo "$r_files") | grep -c . || true)
  common=$(comm -12 <(echo "$h_files") <(echo "$r_files") | grep -c . || true)

  drift=0
  symlinked=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if [[ -L "$home/$f" ]] && [[ "$(abspath "$home/$f")" == "$(abspath "$repo/$f")" ]]; then
      symlinked=$((symlinked + 1))
    elif ! diff -q "$home/$f" "$repo/$f" >/dev/null 2>&1; then
      drift=$((drift + 1))
    fi
  done < <(comm -12 <(echo "$h_files") <(echo "$r_files"))

  if [[ $h_only -eq 0 && $r_only -eq 0 && $drift -eq 0 ]]; then
    if [[ $symlinked -eq $common && $symlinked -gt 0 ]]; then
      echo "  $label: $(c_grn '✓ per-file symlinked') ($common files, all point to repo)"
      warn "    consider  '$(c_bold "bin/sync-skills.sh link")'  to switch to a single dir symlink"
    else
      echo "  $label: $(c_grn '✓ in sync') ($common files)"
    fi
  else
    echo "  $label: $(c_ylw 'drift')  home-only=$h_only, repo-only=$r_only, differ=$drift  ($common matched)"
    if [[ $h_only -gt 0 ]]; then
      cd "$home" && find -L . -maxdepth 2 -type f -name '*.md' 2>/dev/null | sort > /tmp/_h.txt
      cd "$repo" && find -L . -maxdepth 2 -type f -name '*.md' 2>/dev/null | sort > /tmp/_r.txt
      echo "    home-only:"
      comm -23 /tmp/_h.txt /tmp/_r.txt | sed 's|^|      |'
      rm -f /tmp/_h.txt /tmp/_r.txt
    fi
  fi
}

cmd_status() {
  step "Skill / command sync status"
  _status_pair "Skills   " "$HOME_SKILLS" "$REPO_SKILLS"
  _status_pair "Commands" "$HOME_COMMANDS" "$REPO_COMMANDS"

  echo
  if [[ -f "$PLIST_PATH" ]]; then
    if launchctl list "$PLIST_LABEL" >/dev/null 2>&1; then
      ok "auto-sync daemon: loaded ($PLIST_LABEL)"
    else
      warn "auto-sync daemon: plist exists but not loaded"
    fi
  else
    info "auto-sync daemon: $(c_dim 'not installed')"
  fi
  echo
  info "log: $LOG_FILE"
}

_rsync_dir() {
  # $1 = src (with trailing slash), $2 = dst
  # -L follows symlinks so per-file symlinks pointing back at repo become no-ops.
  # --update: skip files that are newer on the receiver
  rsync -aL --update \
    --exclude '.DS_Store' \
    --exclude '.git' \
    --exclude 'processed/' \
    "$1" "$2"
}

cmd_sync() {
  ensure_dirs
  step "Bidirectional sync (newer file wins)"

  # If home dir is already a symlink, sync doesn't apply — nothing to reconcile.
  if [[ -L "$HOME_SKILLS" ]]; then
    ok "skills: symlinked, no sync needed"
  else
    _rsync_dir "$HOME_SKILLS/" "$REPO_SKILLS/"
    _rsync_dir "$REPO_SKILLS/" "$HOME_SKILLS/"
    ok "skills synced"
  fi

  if [[ -L "$HOME_COMMANDS" ]]; then
    ok "commands: symlinked, no sync needed"
  else
    _rsync_dir "$HOME_COMMANDS/" "$REPO_COMMANDS/"
    _rsync_dir "$REPO_COMMANDS/" "$HOME_COMMANDS/"
    ok "commands synced"
  fi
}

_link_one() {
  local label="$1" home="$2" repo="$3"

  # Already fully symlinked at the directory level.
  if [[ -L "$home" ]] && [[ "$(abspath "$home")" == "$(abspath "$repo")" ]]; then
    ok "$label already linked → $repo"
    return
  fi

  ensure_dirs

  # Import anything in home that isn't already in repo.
  # -L follows symlinks so home entries pointing at repo files effectively no-op.
  # --ignore-existing means we never clobber a repo file with a home version
  # (safer default; if you want home-wins, use `sync` first and then `link`).
  if [[ -e "$home" ]] && [[ ! -L "$home" ]]; then
    rsync -aL --ignore-existing \
      --exclude '.DS_Store' --exclude 'processed/' \
      "$home/" "$repo/" 2>/dev/null || true
    info "imported any home-only content into $repo"
  fi

  # Back up and symlink
  local ts backup
  ts="$(date +%Y%m%d-%H%M%S)"
  backup="${home}.bak-${ts}"

  if [[ -e "$home" ]]; then
    mv "$home" "$backup"
    info "backed up $home → $backup"
  fi
  ln -s "$repo" "$home"
  ok "$label linked: $home → $repo"
}

cmd_link() {
  step "Linking home → repo (repo becomes source of truth)"
  _link_one "skills  " "$HOME_SKILLS"   "$REPO_SKILLS"
  _link_one "commands" "$HOME_COMMANDS" "$REPO_COMMANDS"
  echo
  ok "Done. Edit either location — it's the same file now."
  info "Backups (if any) are in ~/.claude/*.bak-<timestamp> and can be safely removed."
}

_unlink_one() {
  local label="$1" home="$2" repo="$3"
  if [[ -L "$home" ]]; then
    rm "$home"
    mkdir -p "$home"
    _rsync_dir "$repo/" "$home/"
    ok "$label unlinked: real copy restored at $home"
  else
    info "$label was not a symlink; nothing to do."
  fi
}

cmd_unlink() {
  step "Unlinking (restoring real directories from repo)"
  _unlink_one "skills  " "$HOME_SKILLS"   "$REPO_SKILLS"
  _unlink_one "commands" "$HOME_COMMANDS" "$REPO_COMMANDS"
}

_write_plist() {
  mkdir -p "$(dirname "$PLIST_PATH")"
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE/sync-skills.sh</string>
    <string>sync</string>
  </array>
  <key>WatchPaths</key>
  <array>
    <string>$HOME_SKILLS</string>
    <string>$HOME_COMMANDS</string>
    <string>$REPO_SKILLS</string>
    <string>$REPO_COMMANDS</string>
  </array>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
}

cmd_install_daemon() {
  step "Installing macOS auto-sync watcher"
  _write_plist
  ok "wrote $PLIST_PATH"
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl load "$PLIST_PATH"
  ok "launchd job loaded"
  info "It runs 'sync-skills.sh sync' whenever any of the four skill/command dirs change."
  info "Log: $LOG_FILE"
}

cmd_uninstall_daemon() {
  step "Uninstalling auto-sync watcher"
  if [[ -f "$PLIST_PATH" ]]; then
    launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
    rm -f "$PLIST_PATH"
    ok "removed $PLIST_PATH"
  else
    info "no plist at $PLIST_PATH — nothing to remove"
  fi
}

cmd_install() {
  step "Full install: link + auto-sync daemon"
  cmd_link
  cmd_install_daemon
  echo
  ok "$(c_bold 'All set.') Skills and commands are now:"
  info "  • single source of truth: the git-versioned repo at $MANAGED_REPO/claude"
  info "  • auto-synced by launchd (even if a symlink is broken later)"
  info "  • visible to both Claude Code CLI and Spidey"
  echo
  info "Run  $(c_bold 'bin/sync-skills.sh status')  any time to double-check."
}

# ---------- dispatch ----------

case "${1:-help}" in
  help|-h|--help)     cmd_help ;;
  status)             cmd_status ;;
  sync)               cmd_sync ;;
  link)               cmd_link ;;
  unlink)             cmd_unlink ;;
  install)            cmd_install ;;
  install-daemon)     cmd_install_daemon ;;
  uninstall-daemon)   cmd_uninstall_daemon ;;
  uninstall)          cmd_uninstall_daemon ;;
  *)
    err "unknown command: $1"
    echo
    cmd_help
    exit 1
    ;;
esac
