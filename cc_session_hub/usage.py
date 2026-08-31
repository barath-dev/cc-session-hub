"""Reads each local Claude Code account's own cached usage/rate-limit data.

Claude Code itself periodically caches rate-limit utilization (from the real API
response headers) into that account's own ~/.claude*/.claude.json under
`cachedUsageUtilization`. This is undocumented/internal and may change shape
across Claude Code versions, but it's just a local read of the user's own
account file on their own disk - the same trust boundary as everything else
in this tool. We degrade gracefully if the field is missing or reshaped.
"""

import glob
import json
import os


def discover_config_dirs() -> list[str]:
    candidates = glob.glob(os.path.expanduser("~/.claude*"))
    return [d for d in candidates if os.path.isfile(os.path.join(d, ".claude.json"))]


def _pct(bucket: dict | None) -> int | None:
    if not bucket:
        return None
    return bucket.get("utilization")


def read_account_usage() -> list[dict]:
    """Returns one row per discovered account with usage info, best-effort."""
    rows = []
    for config_dir in discover_config_dirs():
        claude_json = os.path.join(config_dir, ".claude.json")
        try:
            with open(claude_json) as f:
                data = json.load(f)
        except Exception:
            continue

        oauth = data.get("oauthAccount") or {}
        account = oauth.get("emailAddress")
        if not account:
            base = os.path.basename(config_dir.rstrip("/"))
            account = base[len(".claude-"):] if base.startswith(".claude-") else base

        cached = data.get("cachedUsageUtilization")
        if not cached:
            rows.append({
                "account": account,
                "five_hour_pct": None,
                "five_hour_resets_at": None,
                "seven_day_pct": None,
                "seven_day_resets_at": None,
                "fetched_at_ms": None,
            })
            continue

        util = cached.get("utilization") or {}
        five_hour = util.get("five_hour")
        seven_day = util.get("seven_day")

        rows.append({
            "account": account,
            "five_hour_pct": _pct(five_hour),
            "five_hour_resets_at": (five_hour or {}).get("resets_at"),
            "seven_day_pct": _pct(seven_day),
            "seven_day_resets_at": (seven_day or {}).get("resets_at"),
            "fetched_at_ms": cached.get("fetchedAtMs"),
        })

    return rows
