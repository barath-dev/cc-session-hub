#!/usr/bin/env python3
"""Claude Code hook entrypoint: reports this session's status to cc-session-hub.

Stdlib only (no third-party deps), because this runs inside every Claude Code
hook invocation. Must never block or fail the real session: always exits 0
and swallows every error (hub not running, network hiccup, etc).
"""

import json
import os
import sys
import urllib.request

HUB_URL = os.environ.get("CC_HUB_URL", "http://127.0.0.1:8765/report")
TIMEOUT_SECONDS = 1.5


def resolve_account() -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    config_dir = os.path.expanduser(config_dir)
    claude_json = os.path.join(config_dir, ".claude.json")

    try:
        with open(claude_json) as f:
            data = json.load(f)
        email = (data.get("oauthAccount") or {}).get("emailAddress")
        if email:
            return email
    except Exception:
        pass

    base = os.path.basename(config_dir.rstrip("/"))
    if base.startswith(".claude-"):
        return base[len(".claude-"):]
    return base or "default"


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except Exception:
        return 0

    payload["account"] = resolve_account()
    payload["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR", "")

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            HUB_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
