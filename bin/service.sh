#!/usr/bin/env bash
# ============================================================
# service.sh — manage Spidey as a macOS LaunchAgent
#
# Installs the app so it auto-starts on login and restarts on
# crash. After `install`, you never need to run `./run.sh`
# manually again.
# ============================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_HUB="$(cd "$HERE/.." && pwd)"
CONFIG_PATH="${HUB_CONFIG_PATH:-$AGENT_HUB/config/local.json}"

PLIST_PREFIX="com.myagents"
PORT="8765"
if [[ -f "$CONFIG_PATH" ]]; then
  PLIST_PREFIX="$(jq -r '.agent_hub.plist_label_prefix // "com.myagents"' "$CONFIG_PATH" 2>/dev/null || echo "com.myagents")"
  PORT="$(jq -r '.agent_hub.port // 8765' "$CONFIG_PATH" 2>/dev/null || echo "8765")"
fi

PLIST_LABEL="${PLIST_PREFIX}.hub"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_FILE="/tmp/rr-agents-hub.log"

# ---------- helpers ----------
c_grn() { printf '\033[32m%s\033[0m' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m' "$*"; }
c_red() { printf '\033[31m%s\033[0m' "$*"; }
c_dim() { printf '\033[2m%s\033[0m' "$*"; }
c_bold(){ printf '\033[1m%s\033[0m' "$*"; }
step()  { echo; echo "$(c_bold "▸ $*")"; }
ok()    { echo "  $(c_grn '✓') $*"; }
warn()  { echo "  $(c_ylw '!') $*"; }
info()  { echo "  $*"; }

is_loaded()   { launchctl list "$PLIST_LABEL" >/dev/null 2>&1; }
port_open()   { curl -sS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; }
running_pids(){ pgrep -f "uvicorn app.main:app" 2>/dev/null || true; }

cmd_help() {
  cat <<EOF
$(c_bold "Spidey — Service manager")

$(c_bold "Usage:") bin/service.sh <command>

$(c_bold "Setup:")
  $(c_grn "install")    Install as a launchd LaunchAgent (auto-starts on login,
              restarts on crash). Recommended.
  $(c_grn "uninstall")  Remove the LaunchAgent (and stop the running app).

$(c_bold "Lifecycle:")
  $(c_grn "start")      Load and start the service (if installed).
  $(c_grn "stop")       Stop the service (leaves plist in place).
  $(c_grn "restart")    Stop + start.
  $(c_grn "status")     Show whether the service is loaded and healthy.
  $(c_grn "logs")       Tail /tmp/rr-agents-hub.log.

$(c_bold "Paths:")
  plist:    $PLIST_PATH
  app:      $AGENT_HUB
  log:      $LOG_FILE
  URL:      http://127.0.0.1:$PORT
EOF
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
    <string>$AGENT_HUB/run.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$AGENT_HUB</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST
}

cmd_install() {
  step "Installing Spidey as a LaunchAgent"

  # Kill any manual uvicorn so launchd doesn't hit "Address already in use".
  local pids
  pids="$(running_pids)"
  if [[ -n "$pids" ]]; then
    warn "found existing uvicorn process(es): $pids — stopping"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 2
  fi

  _write_plist
  ok "wrote $PLIST_PATH"

  # Unload if it was already loaded from a previous install (idempotent).
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl load "$PLIST_PATH"
  ok "launchd loaded and started the service"

  echo
  info "Waiting for the app to answer on http://127.0.0.1:$PORT ..."
  local tries=0
  while ! port_open; do
    tries=$((tries + 1))
    if [[ $tries -gt 30 ]]; then
      warn "app didn't respond after 30s — check the log:"
      tail -n 30 "$LOG_FILE" 2>/dev/null || true
      exit 1
    fi
    sleep 1
  done
  ok "app is up: http://127.0.0.1:$PORT"

  echo
  echo "  $(c_bold "All set.") From now on:"
  info "    • the app auto-starts when you log in"
  info "    • it restarts automatically if it crashes"
  info "    • sleep pauses it; wake resumes it (one coalesced catch-up fire)"
  info "    • use 'bin/service.sh status' to check, 'logs' to tail"
}

cmd_uninstall() {
  step "Removing the LaunchAgent"
  if is_loaded; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    ok "launchd unloaded"
  fi
  if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    ok "removed $PLIST_PATH"
  else
    info "no plist to remove"
  fi
  # Clean up any straggler
  local pids
  pids="$(running_pids)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    ok "stopped stray uvicorn process(es)"
  fi
}

cmd_start() {
  step "Starting service"
  if [[ ! -f "$PLIST_PATH" ]]; then
    warn "not installed. Run: bin/service.sh install"
    exit 1
  fi
  if is_loaded; then
    info "already loaded"
  else
    launchctl load "$PLIST_PATH"
    ok "loaded"
  fi
  launchctl start "$PLIST_LABEL" 2>/dev/null || true
  sleep 1
  cmd_status
}

cmd_stop() {
  step "Stopping service"
  if is_loaded; then
    launchctl unload "$PLIST_PATH"
    ok "unloaded (plist preserved)"
  else
    info "not loaded"
  fi
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

cmd_status() {
  step "Service status"
  if [[ -f "$PLIST_PATH" ]]; then
    ok "plist present at $PLIST_PATH"
  else
    warn "not installed. Run: bin/service.sh install"
    return
  fi

  if is_loaded; then
    local pid
    pid="$(launchctl list "$PLIST_LABEL" | awk '/"PID"/ {gsub(/[";,]/, "", $3); print $3}')"
    ok "launchd loaded (label: $PLIST_LABEL)  pid: ${pid:-<transient>}"
  else
    warn "launchd NOT loaded — run: bin/service.sh start"
  fi

  if port_open; then
    ok "app responding at http://127.0.0.1:$PORT"
  else
    warn "app not answering on $PORT — check logs: bin/service.sh logs"
  fi

  info "log: $LOG_FILE"
}

cmd_logs() {
  if [[ ! -f "$LOG_FILE" ]]; then
    warn "no log file yet at $LOG_FILE"
    exit 1
  fi
  tail -n 60 -f "$LOG_FILE"
}

case "${1:-help}" in
  help|-h|--help) cmd_help ;;
  install)        cmd_install ;;
  uninstall)      cmd_uninstall ;;
  start)          cmd_start ;;
  stop)           cmd_stop ;;
  restart)        cmd_restart ;;
  status)         cmd_status ;;
  logs)           cmd_logs ;;
  *) echo "unknown command: $1"; echo; cmd_help; exit 1 ;;
esac
