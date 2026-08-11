#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-3456}"
LOG=/tmp/lg-server.log
PIDFILE=/tmp/lg-server.pid

# Find a Python that actually has localgateway + deps installed.
# (Bare `python3` on this machine is Homebrew 3.13, which has neither.)
find_python() {
  local candidates=(
    ".venv/bin/python3"
    "venv/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python"
    "python3.11"
    "python3.12"
    "python3"
  )
  local c
  for c in "${candidates[@]}"; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
      if "$c" -c "import localgateway, fastapi, uvicorn" >/dev/null 2>&1; then
        printf '%s\n' "$c"
        return 0
      fi
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo "ERROR: no Python interpreter with localgateway + deps installed was found." >&2
  echo "Install with: python3.11 -m pip install -e ." >&2
  exit 1
fi

# Kill any existing instance
pkill -f "localgateway.main" 2>/dev/null || true
sleep 1

echo "Starting LocalGateway on port $PORT (python: $PYTHON)..."
nohup "$PYTHON" -m localgateway.main --port "$PORT" > "$LOG" 2>&1 &
PID=$!

# Verify the server actually comes up (nohup succeeds even if the process crashes instantly)
for _ in $(seq 1 20); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "ERROR: server exited during startup. Last log lines:" >&2
    tail -20 "$LOG" >&2
    rm -f "$PIDFILE"
    exit 1
  fi
  if curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    echo "$PID" > "$PIDFILE"
    echo "Dashboard: http://127.0.0.1:$PORT"
    echo "Logs: $LOG"
    echo "Stop: pkill -f localgateway.main"
    echo "PID: $PID"
    exit 0
  fi
  sleep 0.5
done

echo "WARNING: server process $PID is alive but not responding on port $PORT after 10s." >&2
echo "Last log lines:" >&2
tail -20 "$LOG" >&2
echo "$PID" > "$PIDFILE"
exit 1
