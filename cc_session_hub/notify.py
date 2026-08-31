"""Best-effort macOS desktop notifications (via osascript)."""

import asyncio
import os
import sys

ENABLED = os.environ.get("CC_HUB_NOTIFICATIONS", "1") != "0"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def send(title: str, message: str, subtitle: str | None = None) -> None:
    if not ENABLED or sys.platform != "darwin":
        return

    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    if subtitle:
        script += f' subtitle "{_escape(subtitle)}"'

    try:
        proc = await asyncio.create_subprocess_exec("osascript", "-e", script)
        await proc.wait()
    except Exception:
        pass
