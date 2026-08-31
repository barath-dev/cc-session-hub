#!/usr/bin/env python3
"""Textual TUI for cc-session-hub: live view of all reporting Claude Code sessions."""

import os
from datetime import datetime, timezone

import aiohttp
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

HUB_HTTP = os.environ.get("CC_HUB_HTTP", "http://127.0.0.1:8765")
HUB_WS = os.environ.get("CC_HUB_WS", "ws://127.0.0.1:8765/ws")

STATE_STYLE = {
    "starting": "cyan",
    "working": "bold blue",
    "idle": "grey62",
    "needs_attention": "bold yellow",
    "ended": "dim strike",
}


def _relative_from_dt(then: datetime) -> str:
    delta = datetime.now(timezone.utc) - then
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def relative_time(iso_ts: str | None) -> str:
    if not iso_ts:
        return "-"
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "-"
    return _relative_from_dt(then)


STALE_AFTER_SECONDS = 600


def format_updated(fetched_at_ms: int | None) -> str:
    if not fetched_at_ms:
        return "[grey50]never[/grey50]"
    then = datetime.fromtimestamp(fetched_at_ms / 1000, tz=timezone.utc)
    text = _relative_from_dt(then)
    age = (datetime.now(timezone.utc) - then).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return f"[grey50]{text} (stale)[/grey50]"
    return text


def pct_style(pct: int | None) -> str:
    if pct is None:
        return "grey62"
    if pct >= 80:
        return "bold red"
    if pct >= 50:
        return "bold yellow"
    return "bold green"


def format_pct(pct: int | None) -> str:
    if pct is None:
        return "-"
    style = pct_style(pct)
    return f"[{style}]{pct}%[/{style}]"


def format_resets_in(iso_ts: str | None) -> str:
    if not iso_ts:
        return "-"
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "-"
    delta = then - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "resetting..."
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"in {hours}h{minutes}m"
    return f"in {minutes}m"


class Dashboard(App):
    CSS = """
    #usage {
        height: 6;
        border: solid $accent;
    }
    #detail {
        height: 5;
        border: solid $accent;
        padding: 0 1;
    }
    """
    BINDINGS = [("q", "quit", "Quit")]

    connection_status = reactive("connecting...")

    def __init__(self):
        super().__init__()
        self.sessions: dict[str, dict] = {}
        self.row_keys: dict[str, object] = {}
        self.usage_accounts: dict[str, dict] = {}
        self.usage_row_keys: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield DataTable(id="usage")
            yield DataTable(id="table")
            yield Static("Select a row for details", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        usage_table = self.query_one("#usage", DataTable)
        usage_table.cursor_type = "row"
        usage_table.add_columns("Account", "5h used", "5h resets", "7d used", "7d resets", "Updated")

        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Account", "Project", "State", "Notice", "Tool", "Last update")

        self.set_interval(1.0, self.refresh_times)
        self.run_worker(self.stream_updates(), exclusive=True)

    async def stream_updates(self) -> None:
        async with aiohttp.ClientSession() as http:
            try:
                async with http.get(f"{HUB_HTTP}/sessions") as resp:
                    initial = await resp.json()
                for row in initial:
                    self.upsert_row(row)
            except Exception:
                pass

            try:
                async with http.get(f"{HUB_HTTP}/usage") as resp:
                    accounts = await resp.json()
                for row in accounts:
                    self.upsert_usage_row(row)
            except Exception:
                pass

            while True:
                try:
                    self.connection_status = "connected"
                    async with http.ws_connect(HUB_WS) as ws:
                        self.sub_title = "connected"
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = msg.json()
                                if data.get("type") == "update":
                                    self.upsert_row(data["session"])
                                elif data.get("type") == "usage":
                                    for row in data["accounts"]:
                                        self.upsert_usage_row(row)
                except Exception:
                    self.sub_title = "reconnecting..."
                    await self.sleep(2)

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def upsert_row(self, row: dict) -> None:
        session_id = row["session_id"]
        self.sessions[session_id] = row
        table = self.query_one("#table", DataTable)

        state = row.get("state") or "?"
        style = STATE_STYLE.get(state, "white")
        cells = (
            row.get("account") or "-",
            row.get("project") or "-",
            f"[{style}]{state}[/{style}]",
            row.get("notification_type") or "-",
            row.get("current_tool") or "-",
            relative_time(row.get("updated_at")),
        )

        if session_id in self.row_keys:
            key = self.row_keys[session_id]
            for col, value in zip(table.columns, cells):
                table.update_cell(key, col, value)
        else:
            key = table.add_row(*cells, key=session_id)
            self.row_keys[session_id] = key

    def upsert_usage_row(self, row: dict) -> None:
        account = row["account"]
        self.usage_accounts[account] = row
        table = self.query_one("#usage", DataTable)
        cells = (
            account,
            format_pct(row.get("five_hour_pct")),
            format_resets_in(row.get("five_hour_resets_at")),
            format_pct(row.get("seven_day_pct")),
            format_resets_in(row.get("seven_day_resets_at")),
            format_updated(row.get("fetched_at_ms")),
        )
        if account in self.usage_row_keys:
            key = self.usage_row_keys[account]
            for col, value in zip(table.columns, cells):
                table.update_cell(key, col, value)
        else:
            key = table.add_row(*cells, key=account)
            self.usage_row_keys[account] = key

    def refresh_times(self) -> None:
        table = self.query_one("#table", DataTable)
        last_col = list(table.columns)[-1]
        for session_id, row in self.sessions.items():
            key = self.row_keys.get(session_id)
            if key is not None:
                table.update_cell(key, last_col, relative_time(row.get("updated_at")))

        usage_table = self.query_one("#usage", DataTable)
        cols = list(usage_table.columns)
        five_h_resets_col, seven_d_resets_col, updated_col = cols[2], cols[4], cols[5]
        for account, row in self.usage_accounts.items():
            key = self.usage_row_keys.get(account)
            if key is not None:
                usage_table.update_cell(key, five_h_resets_col, format_resets_in(row.get("five_hour_resets_at")))
                usage_table.update_cell(key, seven_d_resets_col, format_resets_in(row.get("seven_day_resets_at")))
                usage_table.update_cell(key, updated_col, format_updated(row.get("fetched_at_ms")))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        session_id = event.row_key.value
        row = self.sessions.get(session_id)
        detail = self.query_one("#detail", Static)
        if row:
            detail.update(
                f"session: {session_id}\n"
                f"cwd: {row.get('cwd', '-')}\n"
                f"last message: {row.get('last_message') or '-'}"
            )


def main():
    Dashboard().run()


if __name__ == "__main__":
    main()
