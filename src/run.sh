#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UV_BIN="${UV_BIN:-uv}"
DAEMON_PID_FILE="${EMAIL_TRIAGE_DAEMON_PID_FILE:-${XDG_RUNTIME_DIR:-$HOME/.local/run}/email-triage/daemon.pid}"
DAEMON_LOG_FILE="${EMAIL_TRIAGE_DAEMON_LOG_FILE:-${XDG_RUNTIME_DIR:-$HOME/.local/run}/email-triage/daemon.log}"

is_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

cleanup_stale_pidfile() {
  if [[ -f "$DAEMON_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$DAEMON_PID_FILE" 2>/dev/null || true)"
    if [[ -z "$existing_pid" ]] || ! is_alive "$existing_pid"; then
      rm -f "$DAEMON_PID_FILE"
    fi
  fi
}

stop_daemon() {
  cleanup_stale_pidfile
  if [[ ! -f "$DAEMON_PID_FILE" ]]; then
    echo "No daemon process is currently tracked."
    return 0
  fi

  local pid
  pid="$(cat "$DAEMON_PID_FILE")"
  echo "Stopping daemon (pid: $pid)..."
  kill "$pid" 2>/dev/null || true

  local i
  for i in {1..10}; do
    if ! is_alive "$pid"; then
      rm -f "$DAEMON_PID_FILE"
      echo "Daemon stopped."
      return 0
    fi
    sleep 1
  done

  echo "Daemon did not stop gracefully. Sending SIGKILL..."
  kill -9 "$pid" 2>/dev/null || true
  if ! is_alive "$pid"; then
    rm -f "$DAEMON_PID_FILE"
    echo "Daemon stopped."
    return 0
  fi

  echo "Unable to stop daemon with pid $pid."
  return 1
}

start_daemon() {
  local daemon_cmd=("$@")
  cleanup_stale_pidfile
  if [[ -f "$DAEMON_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$DAEMON_PID_FILE")"
    if is_alive "$existing_pid"; then
      echo "Daemon is already running with pid $existing_pid."
      return 1
    fi
    rm -f "$DAEMON_PID_FILE"
  fi

  local pid_dir
  pid_dir="$(dirname "$DAEMON_PID_FILE")"
  mkdir -p "$pid_dir"
  mkdir -p "$(dirname "$DAEMON_LOG_FILE")"

  ("${daemon_cmd[@]}") >>"$DAEMON_LOG_FILE" 2>&1 &
  local daemon_pid=$!
  echo "$daemon_pid" > "$DAEMON_PID_FILE"

  if is_alive "$daemon_pid"; then
    echo "Daemon started with pid $daemon_pid."
    echo "Log: $DAEMON_LOG_FILE"
    return 0
  fi

  rm -f "$DAEMON_PID_FILE"
  echo "Failed to start daemon. Check log at $DAEMON_LOG_FILE."
  return 1
}

status_daemon() {
  local config_path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        config_path="${2:-}"
        shift 2
        ;;
      *)
        break
        ;;
    esac
  done

  local state_db
  local model
  local reasoning_effort
  local status_payload
  status_payload="$("$UV_BIN" run --project "$REPO_ROOT" python - "$config_path" <<'PY'
from pathlib import Path
import sys

from src.common import load_config
from src.triage_cycle import normalize_automation_settings, normalize_ai_settings

config_path = sys.argv[1] or None
config, _ = load_config(config_path)
automation = normalize_automation_settings(config)
ai = normalize_ai_settings(config)
state_db = Path(str(automation.get("state_db", "~/.config/email-triage/triage.db"))).expanduser()
print(state_db)
print(ai.get("model", ""))
print(ai.get("reasoning_effort", ""))
PY
)"

  mapfile -t status_payload <<<"$status_payload"
  state_db="${status_payload[0]}"
  model="${status_payload[1]}"
  reasoning_effort="${status_payload[2]}"

  if [[ -z "$state_db" ]]; then
    echo "Failed to resolve triage state database path."
    return 1
  fi

  cleanup_stale_pidfile
  if [[ -f "$DAEMON_PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$DAEMON_PID_FILE")"
    if is_alive "$existing_pid"; then
      echo "daemon running (pid: $existing_pid)"
    else
      echo "daemon stopped"
    fi
  else
  echo "daemon stopped"
  fi

  echo "database: $state_db"
  echo "model: ${model:-default}"
  echo "reasoning_effort: ${reasoning_effort:-default}"
  echo "log: $DAEMON_LOG_FILE"

  "$UV_BIN" run --project "$REPO_ROOT" python - "$state_db" <<'PY'
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

state_db = Path(sys.argv[1])
conn = sqlite3.connect(str(state_db))
conn.row_factory = sqlite3.Row
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS triage_state (
      email_id TEXT PRIMARY KEY,
      subject TEXT,
      sender TEXT,
      sender_email TEXT,
      received_at TEXT,
      priority TEXT,
      actionable INTEGER NOT NULL,
      reason TEXT,
      summary TEXT,
      reply_text TEXT,
      drafted INTEGER NOT NULL DEFAULT 0,
      draft_id TEXT,
      status TEXT NOT NULL,
      error TEXT,
      raw_email TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """
)
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS triage_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_at TEXT NOT NULL,
      mode TEXT NOT NULL,
      emails_seen INTEGER NOT NULL,
      triaged_count INTEGER NOT NULL,
      drafted_count INTEGER NOT NULL,
      skipped_count INTEGER NOT NULL,
      error_count INTEGER NOT NULL,
      details_json TEXT
    )
    """
)

def scalar_value(query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0] or 0)


def format_local_time(value: str) -> str:
    if not value or value == "n/a":
        return value or "n/a"

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")

def status_value(label: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS value FROM triage_state WHERE status = ?", (label,)).fetchone()
    return int(row[0] or 0)

seen = scalar_value("SELECT COUNT(*) FROM triage_state")
triaged = status_value("triaged")
drafted = status_value("drafted")
skipped = status_value("skipped")
archived = status_value("archived")
errors = status_value("error")
last_state_update = conn.execute("SELECT MAX(updated_at) FROM triage_state").fetchone()[0] or "n/a"

run_row = conn.execute(
    """
    SELECT run_at, mode, emails_seen, triaged_count, drafted_count, skipped_count, error_count
    FROM triage_runs ORDER BY id DESC LIMIT 1
    """
).fetchone()

print(f"seen={seen}")
print(f"triaged={triaged}")
print(f"drafted={drafted}")
print(f"skipped={skipped}")
print(f"archived={archived}")
print(f"errors={errors}")
print(f"last_state_update={format_local_time(last_state_update)}")

if run_row is None:
    print("last_run=n/a")
else:
    local_run_at = format_local_time(run_row["run_at"])
    print(
        "last_run run_at={0} mode={1} seen={2} triaged={3} drafted={4} skipped={5} errors={6}".format(
            local_run_at,
            run_row["mode"],
            run_row["emails_seen"],
            run_row["triaged_count"],
            run_row["drafted_count"],
            run_row["skipped_count"],
            run_row["error_count"],
        )
    )
PY
}

reset_status() {
  local config_path=""
  local state_db=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        if [[ $# -lt 2 ]]; then
          echo "Error: --config requires a value." >&2
          return 1
        fi
        config_path="$2"
        shift 2
        ;;
      --state-db)
        if [[ $# -lt 2 ]]; then
          echo "Error: --state-db requires a value." >&2
          return 1
        fi
        state_db="$2"
        shift 2
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        return 1
        ;;
    esac
  done

  if [[ -z "$state_db" ]]; then
    state_db="$($UV_BIN run --project "$REPO_ROOT" python - "$config_path" <<'PY'
from pathlib import Path
import sys

from src.common import load_config
from src.triage_cycle import normalize_automation_settings

config_path = sys.argv[1] or None
config, _ = load_config(config_path)
automation = normalize_automation_settings(config)
state_db = Path(str(automation.get("state_db", "~/.config/email-triage/triage.db"))).expanduser()
print(state_db)
PY
 )"
  fi

  if [[ -z "$state_db" ]]; then
    echo "Failed to resolve triage state database path."
    return 1
  fi

  "$UV_BIN" run --project "$REPO_ROOT" python - "$state_db" <<'PY'
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

state_db = Path(sys.argv[1])
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
conn = sqlite3.connect(str(state_db))
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS triage_state (
      email_id TEXT PRIMARY KEY,
      subject TEXT,
      sender TEXT,
      sender_email TEXT,
      received_at TEXT,
      priority TEXT,
      actionable INTEGER NOT NULL,
      reason TEXT,
      summary TEXT,
      reply_text TEXT,
      drafted INTEGER NOT NULL DEFAULT 0,
      draft_id TEXT,
      status TEXT NOT NULL,
      error TEXT,
      raw_email TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """
)
result = conn.execute("UPDATE triage_state SET status = 'triaged', updated_at = ?", (now,))
print(f"Reset {result.rowcount} rows to triaged status.")
conn.commit()
PY
}

usage() {
  cat <<'EOF'
Usage:
  ./src/run.sh [mode] [extra-args...]

Modes:
  once          One triage cycle with Codex + draft creation (default)
  dry           One triage cycle with Codex, no draft creation
  daemon        Continuous Codex triage + drafting loop (start/stop)
  daemon start  Start daemon (explicit)
  daemon status Check daemon status
  daemon stop   Stop daemon
  reset-status  Reset triage state status values to triaged
  daemon-dry    Continuous Codex triage loop, no draft creation
  rules         One rule-only cycle with draft creation (no Codex calls)
  rules-daemon  Continuous rule-only triage + drafting loop
  help          Show this help

Examples:
  ./src/run.sh
  ./src/run.sh dry --limit 10
  ./src/run.sh daemon --interval-seconds 900
  ./src/run.sh daemon stop
  ./src/run.sh daemon status
  ./src/run.sh reset-status
  ./src/run.sh --reset-status
  ./src/run.sh rules --limit 20
EOF
}

mode="once"
if [[ $# -gt 0 ]]; then
  case "$1" in
      once|dry|daemon|daemon-dry|rules|rules-daemon|reset-status|help|-h|--help)
        mode="$1"
        shift
        ;;
      --reset-status)
        mode="reset-status"
        shift
        ;;
      -*)
        ;;
      *)
        echo "Unknown mode: $1" >&2
        usage
        exit 1
        ;;
    esac
  fi

if [[ "$mode" == "help" || "$mode" == "-h" || "$mode" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "Error: uv is required but not found in PATH. Install uv: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if [[ "$mode" != rules* ]]; then
  has_key="false"
  has_login="false"

  if [[ -n "${OPENAI_API_KEY:-}" || -n "${CODEX_API_KEY:-}" ]]; then
    has_key="true"
  fi

  if command -v codex >/dev/null 2>&1; then
    if codex login status 2>&1 | grep -qi "logged in"; then
      has_login="true"
    fi
  fi

  if [[ "$has_key" != "true" && "$has_login" != "true" ]]; then
    echo "Warning: no Codex auth detected. Run 'codex login' (subscription) or set OPENAI_API_KEY." >&2
  fi
fi

case "$mode" in
  once)
    exec "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/triage_cycle.py" --apply "$@"
    ;;
  dry)
    exec "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/triage_cycle.py" "$@"
  ;;
  daemon)
    case "${1-}" in
      start)
        shift
        start_daemon "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/daemon.py" "$@"
        ;;
      status)
        shift
        status_daemon "$@"
        ;;
      stop)
        stop_daemon
        ;;
      ""|--*)
        start_daemon "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/daemon.py" "$@"
        ;;
      *)
        start_daemon "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/daemon.py" "$@"
        ;;
    esac
    ;;
  daemon-dry)
    exec "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/daemon.py" --dry-run "$@"
    ;;
  rules)
    exec "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/triage_cycle.py" --apply --no-codex "$@"
    ;;
  rules-daemon)
    exec "$UV_BIN" run --project "$REPO_ROOT" "$SCRIPT_DIR/daemon.py" --no-codex "$@"
    ;;
  reset-status)
    reset_status "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac
