#!/usr/bin/env bash
# Starts the cc-session-hub daemon (if not already running) and opens the TUI dashboard.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
HUB_LOG="$PROJECT_DIR/hub.log"
HUB_URL="http://127.0.0.1:8765/sessions"

if ! curl -s -o /dev/null -m 1 "$HUB_URL"; then
    echo "Starting cc-session-hub..."
    nohup "$PYTHON" "$PROJECT_DIR/hub.py" > "$HUB_LOG" 2>&1 &
    disown
    for _ in $(seq 1 20); do
        sleep 0.2
        curl -s -o /dev/null -m 1 "$HUB_URL" && break
    done
fi

exec "$PYTHON" "$PROJECT_DIR/tui/dashboard.py"
