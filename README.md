# cc-session-hub

A local, cross-account status dashboard for Claude Code. Each Claude Code session
(regardless of which Anthropic account it's logged into) cooperatively reports its
own status to a local hub via hooks; a terminal dashboard shows all of them live.

Read-only monitoring only — nothing reaches into another session's private state.
Every session only ever pushes its own status out.

## How it works

- `hub.py` runs a small `aiohttp` server on `127.0.0.1:8765` (localhost only) with a
  SQLite-backed `sessions` table.
- Each Claude Code account's `settings.json` gets a `hooks` block that calls
  `cc_session_hub/report_hook.py` on `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
  `Stop`, `Notification`, and `SessionEnd`.
- `report_hook.py` reads the hook JSON from stdin, figures out which account it's
  running under (via `$CLAUDE_CONFIG_DIR`), and POSTs a small status update to the hub.
  It always exits 0 and swallows every error — if the hub isn't running, your Claude
  Code session is completely unaffected.
- `tui/dashboard.py` is a Textual app that connects to the hub over WebSocket and shows
  a live table of every session that has reported in.
- The hub also polls each account's own `~/.claude*/.claude.json` every 30s for Claude
  Code's own cached rate-limit data (`cachedUsageUtilization` — undocumented/internal,
  but it's just a local read of your own account file) and shows 5-hour and 7-day usage
  % + reset times per account at the top of the dashboard, plus how long ago that data
  was last refreshed (an account you haven't touched in a while will show as "stale").
- The hub fires a native macOS notification (`osascript`) when a session's `Notification`
  hook reports `permission_prompt`, `quota_auto_resume_fired`, or
  `quota_auto_resume_disabled` — the moments you'd actually want to be pulled away from
  what you're doing. Everything else (e.g. `idle_prompt`, which fires every ~60s of
  inactivity) is deliberately excluded as too noisy. Set `CC_HUB_NOTIFICATIONS=0` before
  starting the hub to disable notifications entirely.

## Setup

```bash
cd ~/tools/cc-session-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 1. Start the hub

```bash
.venv/bin/python hub.py
```

Leave this running (see "Run at login" below to make this automatic).

### 2. Wire hooks into each account's settings.json

If you run Claude Code under multiple accounts (e.g. via `CLAUDE_CONFIG_DIR=~/.claude-work
claude`), each has its own `settings.json`. Add the block below to every account's
`settings.json` you want visible in the dashboard — merge it into any existing `hooks`
key rather than overwrite it. If you only use one account, just add it to
`~/.claude/settings.json`.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "$HOME/tools/cc-session-hub/.venv/bin/python $HOME/tools/cc-session-hub/cc_session_hub/report_hook.py", "async": true }] }
    ]
  }
}
```

If you didn't clone this into `~/tools/cc-session-hub`, adjust the path accordingly (or
use `${CLAUDE_PROJECT_DIR}`-style absolute paths — `$HOME` expansion depends on hooks
running through a shell, which is the default).

The same block works verbatim in every account's `settings.json` — the script figures
out which account it is at report time from `$CLAUDE_CONFIG_DIR`. Drop the `PreToolUse`
entry if you don't want a per-tool-call report (it's the noisiest one; everything else
is low-frequency).

### 3. Run the dashboard

```bash
.venv/bin/python tui/dashboard.py
```

Press `q` to quit.

Or use `./open.sh`, which starts the hub automatically if it isn't already running and
then opens the dashboard — handy to alias (e.g. `alias cc-dashboard="~/tools/cc-session-hub/open.sh"`)
so you can just type one command.

## Run the hub at login (optional)

```bash
sed -e "s|__VENV_PYTHON__|$HOME/tools/cc-session-hub/.venv/bin/python|" \
    -e "s|__HUB_PY__|$HOME/tools/cc-session-hub/hub.py|" \
    -e "s|__PROJECT_DIR__|$HOME/tools/cc-session-hub|g" \
    launchd/com.user.cc-session-hub.plist > ~/Library/LaunchAgents/com.user.cc-session-hub.plist

launchctl load ~/Library/LaunchAgents/com.user.cc-session-hub.plist
```

To stop/unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.cc-session-hub.plist
```

## Configuration

- `CC_HUB_URL` (used by `report_hook.py`) — override the hub's report endpoint, default
  `http://127.0.0.1:8765/report`.
- `CC_HUB_HTTP` / `CC_HUB_WS` (used by `tui/dashboard.py`) — override the hub's base HTTP/WS
  URL, defaults `http://127.0.0.1:8765` / `ws://127.0.0.1:8765/ws`.

## Privacy

The hub binds to `127.0.0.1` only — it's never reachable from the network. It stores,
locally on this machine only: session id, account email/label, current working
directory, a short truncated snippet of your last assistant message, and coarse status
(idle/working/needs attention). Delete `sessions.db` any time to clear history.
