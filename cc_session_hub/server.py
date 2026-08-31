"""aiohttp hub: ingest hook reports, serve snapshots, broadcast live updates."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiohttp import web, WSMsgType

from . import db, notify, usage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cc-session-hub")

HOST = "127.0.0.1"
PORT = 8765

MAX_MESSAGE_LEN = 200
USAGE_POLL_SECONDS = 30

STATE_BY_EVENT = {
    "SessionStart": "starting",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "Stop": "idle",
    "Notification": "needs_attention",
    "SessionEnd": "ended",
}

# notification_type values worth a desktop notification for. Deliberately
# excludes idle_prompt (fires ~every 60s of inactivity - far too noisy).
NOTIFY_ON_TYPE = {
    "permission_prompt": "Waiting on a permission approval",
    "quota_auto_resume_fired": "Usage limit hit — auto-resume scheduled",
    "quota_auto_resume_disabled": "Usage limit hit — auto-resume is disabled",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_row(payload: dict) -> dict:
    event = payload.get("hook_event_name", "")
    cwd = payload.get("cwd")
    row = {
        "session_id": payload["session_id"],
        "account": payload.get("account", "unknown"),
        "cwd": cwd,
        "project": cwd.rstrip("/").split("/")[-1] if cwd else None,
        "state": STATE_BY_EVENT.get(event),
        "updated_at": now_iso(),
    }

    if event == "SessionStart":
        row["session_start_reason"] = payload.get("session_start_reason")
        row["notification_type"] = None
        row["current_tool"] = None
    elif event == "PreToolUse":
        row["current_tool"] = payload.get("tool_name")
    elif event == "Stop":
        row["current_tool"] = None
        msg = payload.get("last_assistant_message") or ""
        row["last_message"] = msg[:MAX_MESSAGE_LEN]
    elif event == "Notification":
        row["notification_type"] = payload.get("notification_type")
    elif event == "SessionEnd":
        row["ended_at"] = now_iso()
        row["current_tool"] = None
    elif event == "UserPromptSubmit":
        row["notification_type"] = None

    return row


class Hub:
    def __init__(self):
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.last_usage: list[dict] = []
        self.background_tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def broadcast_json(self, message: dict):
        if not self.ws_clients:
            return
        text = json.dumps(message)
        dead = set()
        for ws in self.ws_clients:
            try:
                await ws.send_str(text)
            except ConnectionResetError:
                dead.add(ws)
        self.ws_clients -= dead

    async def broadcast(self, session_row: dict):
        await self.broadcast_json({"type": "update", "session": session_row})

    async def poll_usage_loop(self):
        while True:
            try:
                rows = await asyncio.to_thread(usage.read_account_usage)
                if rows != self.last_usage:
                    self.last_usage = rows
                    await self.broadcast_json({"type": "usage", "accounts": rows})
            except Exception:
                log.exception("usage poll failed")
            await asyncio.sleep(USAGE_POLL_SECONDS)

    async def handle_report(self, request: web.Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        if "session_id" not in payload or "hook_event_name" not in payload:
            return web.json_response({"error": "missing required fields"}, status=400)

        row = build_row(payload)
        conn = db.connect()
        try:
            merged = db.upsert_session(conn, row)
        finally:
            conn.close()

        await self.broadcast(merged)

        event = payload.get("hook_event_name")
        notif_type = payload.get("notification_type")
        if event == "Notification" and notif_type in NOTIFY_ON_TYPE:
            self.spawn(notify.send(
                title=f"Claude Code needs you — {merged.get('account', 'unknown')}",
                message=NOTIFY_ON_TYPE[notif_type],
                subtitle=merged.get("project"),
            ))

        return web.json_response({"ok": True})

    async def handle_sessions(self, request: web.Request):
        conn = db.connect()
        try:
            rows = db.list_sessions(conn)
        finally:
            conn.close()
        return web.json_response(rows)

    async def handle_usage(self, request: web.Request):
        if not self.last_usage:
            self.last_usage = await asyncio.to_thread(usage.read_account_usage)
        return web.json_response(self.last_usage)

    async def handle_ws(self, request: web.Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        log.info("dashboard connected (%d total)", len(self.ws_clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.ws_clients.discard(ws)
            log.info("dashboard disconnected (%d total)", len(self.ws_clients))
        return ws


def build_app() -> web.Application:
    hub = Hub()
    app = web.Application()
    app.router.add_post("/report", hub.handle_report)
    app.router.add_get("/sessions", hub.handle_sessions)
    app.router.add_get("/usage", hub.handle_usage)
    app.router.add_get("/ws", hub.handle_ws)

    async def start_background_tasks(app):
        app["usage_poller"] = asyncio.create_task(hub.poll_usage_loop())

    async def cleanup_background_tasks(app):
        app["usage_poller"].cancel()

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app


def main():
    app = build_app()
    log.info("cc-session-hub listening on http://%s:%d", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
