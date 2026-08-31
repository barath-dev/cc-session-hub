#!/usr/bin/env python3
"""Textual TUI for cc-session-hub: live view of all reporting Claude Code sessions."""

import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

import aiohttp
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

HUB_HTTP = os.environ.get("CC_HUB_HTTP", "http://127.0.0.1:8765")
HUB_WS = os.environ.get("CC_HUB_WS", "ws://127.0.0.1:8765/ws")

# Palette: warm phosphor-amber terminal, not the generic near-black+neon look.
# Named tokens used consistently everywhere instead of ad hoc color names.
INK = "#14110D"       # background
PANEL = "#1C1712"     # panel/table surface, one step up from INK
HIGHLIGHT = "#332A1D" # cursor row - lighter than PANEL, but neutral so
                       # per-cell semantic colors (bars, state) stay legible
PAPER = "#EAE3D8"     # primary text
SLATE = "#8A8378"     # muted/idle text
DIM = "#55504A"       # borders, disabled/very-quiet text
AMBER = "#E7A33E"     # primary accent - activity, focus, brand
ALERT = "#E2604B"     # needs-attention / high usage, used sparingly
SAGE = "#8FB88A"      # healthy / low usage

STATE_META = {
    "starting": ("◌", AMBER, "starting"),
    "working": ("●", AMBER, "working"),
    "idle": ("○", SLATE, "idle"),
    "needs_attention": ("▲", ALERT, "attention"),
    "ended": ("·", DIM, "ended"),
}


def render_state(state: str | None, pulse_on: bool = False) -> str:
    glyph, color, label = STATE_META.get(state, ("?", SLATE, state or "?"))
    if state == "working" and pulse_on:
        glyph = "◉"
    return f"[{color}]{glyph} {label}[/{color}]"


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
        return f"[{DIM}]never[/{DIM}]"
    then = datetime.fromtimestamp(fetched_at_ms / 1000, tz=timezone.utc)
    text = _relative_from_dt(then)
    age = (datetime.now(timezone.utc) - then).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return f"[{DIM}]{text} (stale)[/{DIM}]"
    return text


BAR_WIDTH = 10


def pct_color(pct: int) -> str:
    if pct >= 80:
        return ALERT
    if pct >= 50:
        return AMBER
    return SAGE


def format_pct(pct: int | None) -> str:
    if pct is None:
        return f"[{DIM}]{'░' * BAR_WIDTH} –[/{DIM}]"
    color = pct_color(pct)
    filled = round(BAR_WIDTH * min(pct, 100) / 100)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    return f"[{color}]{bar} {pct}%[/{color}]"


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


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def spawn_new_terminal_window(cwd: str, config_dir: str, session_id: str | None) -> str | None:
    """Opens a new Terminal.app window running claude (fresh, or --resume
    session_id) in cwd under config_dir. Returns an error message, or None on
    success. Doesn't touch this process - the dashboard keeps running."""
    if sys.platform != "darwin":
        return "New-window launching only supports macOS Terminal.app right now"

    claude_cmd = "claude"
    if config_dir:
        claude_cmd = f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} {claude_cmd}"
    if session_id:
        claude_cmd += f" --resume {shlex.quote(session_id)}"

    shell_cmd = f"cd {shlex.quote(cwd)} && {claude_cmd}"
    script = f'tell application "Terminal" to do script "{_applescript_escape(shell_cmd)}"'

    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except Exception as e:
        return str(e)
    if result.returncode != 0:
        return result.stderr.strip() or "osascript failed"
    return None


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation, dismissed with the chosen bool."""

    CSS = f"""
    ConfirmScreen {{
        align: center middle;
        background: {INK} 60%;
    }}
    #confirm-box {{
        width: 64;
        height: auto;
        border: heavy {ALERT};
        padding: 1 2;
        background: {PANEL};
        color: {PAPER};
    }}
    #confirm-hint {{
        color: {SLATE};
        margin-top: 1;
    }}
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(self.message)
            yield Static(f"[{AMBER}]y[/{AMBER}] to confirm   [{AMBER}]n[/{AMBER}] / Esc to cancel", id="confirm-hint")

    def on_mount(self) -> None:
        self.query_one("#confirm-box").border_title = "confirm"

    def on_key(self, event) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)


DIR_MATCH_LIMIT = 20


def list_matching_dirs(value: str) -> tuple[str, list[str]]:
    """For a partially-typed path, returns (prefix-to-keep, [matching subdirectory
    names]) - the part of `value` before the final path segment, and every real
    subdirectory of that location whose name starts with what's typed so far."""
    if not value:
        return "", []

    expanded = os.path.expanduser(value)
    if expanded.endswith(os.sep):
        fs_dir, typed = expanded, ""
    else:
        fs_dir, typed = os.path.split(expanded)
        fs_dir = fs_dir or "."

    try:
        entries = sorted(os.listdir(fs_dir))
    except OSError:
        return "", []

    matches = [
        e for e in entries
        if e.startswith(typed) and os.path.isdir(os.path.join(fs_dir, e))
    ]
    original_prefix = value if not typed else value[: len(value) - len(typed)]
    return original_prefix, matches[:DIR_MATCH_LIMIT]


class PathSuggester(Suggester):
    """Ghost inline completion (best match) as you type, filesystem-backed."""

    def __init__(self):
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        prefix, matches = list_matching_dirs(value)
        if not matches:
            return None
        return prefix + matches[0] + os.sep


class NewSessionScreen(ModalScreen[str | None]):
    """Prompts for a directory, dismissed with the resolved absolute path or None."""

    CSS = f"""
    NewSessionScreen {{
        align: center middle;
        background: {INK} 60%;
    }}
    #new-box {{
        width: 70;
        height: auto;
        border: heavy {AMBER};
        padding: 1 2;
        background: {PANEL};
        color: {PAPER};
    }}
    #dir-input {{
        border: round {DIM};
    }}
    #dir-input:focus {{
        border: round {AMBER};
    }}
    #dir-input > .input--suggestion {{
        color: {SLATE};
        text-style: italic;
    }}
    #dir-options {{
        max-height: 5;
        margin-top: 1;
        border: round {DIM};
        background: {INK};
    }}
    #dir-options:focus {{
        border: round {AMBER};
    }}
    #new-error {{
        color: {ALERT};
        margin-top: 1;
    }}
    #new-hint {{
        color: {SLATE};
        margin-top: 1;
    }}
    """

    def __init__(self, account: str, default_dir: str):
        super().__init__()
        self.account = account
        self.default_dir = default_dir

    def compose(self) -> ComposeResult:
        with Container(id="new-box"):
            yield Static(f"new session — [{AMBER}]{self.account}[/{AMBER}]")
            yield Input(value=self.default_dir, id="dir-input", suggester=PathSuggester())
            yield OptionList(id="dir-options")
            yield Static("", id="new-error")
            yield Static(
                f"[{AMBER}]enter[/{AMBER}] start   [{AMBER}]tab[/{AMBER}] complete   "
                f"[{AMBER}]↓[/{AMBER}] browse   [{AMBER}]esc[/{AMBER}] cancel",
                id="new-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#new-box").border_title = "new session"
        self.query_one("#dir-options", OptionList).display = False
        self.query_one("#dir-input", Input).focus()

    def _refresh_dropdown(self, value: str) -> None:
        _, matches = list_matching_dirs(value)
        options = self.query_one("#dir-options", OptionList)
        options.clear_options()
        if matches:
            options.add_options(matches)
            options.display = True
        else:
            options.display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "dir-input":
            self.query_one("#new-error", Static).update("")
            self._refresh_dropdown(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        path = os.path.expanduser(event.value.strip())
        if not path or not os.path.isdir(path):
            self.query_one("#new-error", Static).update(f"No such directory: {path or '(empty)'}")
            return
        self.dismiss(os.path.abspath(path))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "dir-options":
            return
        dir_input = self.query_one("#dir-input", Input)
        prefix, _ = list_matching_dirs(dir_input.value)
        new_value = prefix + str(event.option.prompt) + os.sep
        dir_input.value = new_value
        dir_input.cursor_position = len(new_value)
        dir_input.focus()
        self._refresh_dropdown(new_value)

    def on_key(self, event) -> None:
        options = self.query_one("#dir-options", OptionList)
        if event.key == "escape":
            if self.focused is options:
                options.display = False
                self.query_one("#dir-input", Input).focus()
            else:
                self.dismiss(None)
            event.stop()
        elif event.key == "down" and isinstance(self.focused, Input) and options.display:
            options.focus()
            if options.highlighted is None and options.option_count:
                options.highlighted = 0
            event.stop()
        elif event.key == "tab" and isinstance(self.focused, Input):
            suggestion = self.focused._suggestion
            if suggestion:
                self.focused.value = suggestion
                self.focused.cursor_position = len(suggestion)
                self._refresh_dropdown(suggestion)
            event.stop()
            event.prevent_default()


class Dashboard(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = f"""
    Screen {{
        background: {INK};
        color: {PAPER};
    }}
    Header {{
        background: {INK};
        color: {AMBER};
        text-style: bold;
    }}
    Footer {{
        background: {INK};
        color: {SLATE};
    }}
    Footer > .footer-key--key {{
        color: {AMBER};
        text-style: bold;
    }}
    #usage {{
        height: 7;
        border: heavy {AMBER};
        background: {PANEL};
    }}
    #usage > .datatable--header {{
        background: {PANEL};
        color: {AMBER};
        text-style: bold;
    }}
    #table {{
        border: heavy {DIM};
        background: {PANEL};
    }}
    #table > .datatable--header {{
        background: {PANEL};
        color: {SLATE};
        text-style: bold;
    }}
    DataTable > .datatable--cursor {{
        background: {HIGHLIGHT};
        text-style: bold;
    }}
    DataTable > .datatable--odd-row {{
        background: {INK};
    }}
    #detail {{
        height: 5;
        border: round {DIM};
        padding: 0 1;
        background: {PANEL};
        color: {SLATE};
    }}
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("o", "open_session", "Open"),
        ("n", "new_session", "New"),
    ]

    connection_status = reactive("connecting...")

    def __init__(self):
        super().__init__()
        self.sessions: dict[str, dict] = {}
        self.row_keys: dict[str, object] = {}
        self.usage_accounts: dict[str, dict] = {}
        self.usage_row_keys: dict[str, object] = {}
        self.selected_session_id: str | None = None
        self.selected_account: str | None = None
        self._pulse_on = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield DataTable(id="usage", zebra_stripes=True)
            yield DataTable(id="table", zebra_stripes=True)
            yield Static("select a row for details", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "cc-session-hub"
        self.sub_title = "connecting…"

        usage_table = self.query_one("#usage", DataTable)
        usage_table.cursor_type = "row"
        usage_table.border_title = "accounts"
        usage_table.add_columns("Account", "5h used", "5h resets", "7d used", "7d resets", "Updated")

        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.border_title = "sessions"
        table.add_columns("Account", "Project", "State", "Notice", "Tool", "Last update")

        self.query_one("#detail", Static).border_title = "detail"

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
                        self.sub_title = "● live"
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = msg.json()
                                if data.get("type") == "update":
                                    self.upsert_row(data["session"])
                                elif data.get("type") == "usage":
                                    for row in data["accounts"]:
                                        self.upsert_usage_row(row)
                except Exception:
                    self.sub_title = "○ reconnecting…"
                    await self.sleep(2)

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def upsert_row(self, row: dict) -> None:
        session_id = row["session_id"]
        self.sessions[session_id] = row
        table = self.query_one("#table", DataTable)

        cells = (
            row.get("account") or "-",
            row.get("project") or "-",
            render_state(row.get("state"), self._pulse_on),
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
        table.border_subtitle = f"{len(self.row_keys)} tracked"

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
        table.border_subtitle = f"{len(self.usage_row_keys)} accounts"

    def refresh_times(self) -> None:
        self._pulse_on = not self._pulse_on

        table = self.query_one("#table", DataTable)
        cols = list(table.columns)
        state_col, last_col = cols[2], cols[-1]
        for session_id, row in self.sessions.items():
            key = self.row_keys.get(session_id)
            if key is not None:
                table.update_cell(key, last_col, relative_time(row.get("updated_at")))
                if row.get("state") == "working":
                    table.update_cell(key, state_col, render_state("working", self._pulse_on))

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
                f"[{SLATE}]session[/{SLATE}] {session_id}\n"
                f"[{SLATE}]cwd[/{SLATE}]     {row.get('cwd', '-')}\n"
                f"[{SLATE}]last[/{SLATE}]    {row.get('last_message') or '-'}"
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        if event.data_table.id == "table":
            self.selected_session_id = event.row_key.value
        elif event.data_table.id == "usage":
            self.selected_account = event.row_key.value

    def _focused_account(self) -> tuple[str | None, str | None]:
        """Which account 'n' should act on: the highlighted usage row if that
        table has focus, otherwise the account of the highlighted session."""
        focused = self.focused
        if isinstance(focused, DataTable) and focused.id == "usage" and self.selected_account:
            row = self.usage_accounts.get(self.selected_account)
            return self.selected_account, (row or {}).get("config_dir")
        if self.selected_session_id:
            row = self.sessions.get(self.selected_session_id)
            if row:
                return row.get("account"), row.get("config_dir")
        if self.selected_account:
            row = self.usage_accounts.get(self.selected_account)
            return self.selected_account, (row or {}).get("config_dir")
        return None, None

    def action_open_session(self) -> None:
        session_id = self.selected_session_id
        if not session_id:
            return
        row = self.sessions.get(session_id)
        if not row or not row.get("cwd"):
            self.notify("No directory known for this session", severity="warning")
            return

        def proceed() -> None:
            error = spawn_new_terminal_window(row["cwd"], row.get("config_dir") or "", session_id)
            if error:
                self.notify(f"Couldn't open a new window: {error}", severity="error")
            else:
                self.notify(f"Opened {row.get('project') or session_id} in a new Terminal window")

        if row.get("state") != "ended":
            def handle_result(confirmed: bool | None) -> None:
                if confirmed:
                    proceed()

            self.push_screen(
                ConfirmScreen(
                    f"Session in {row.get('project') or row['cwd']} looks still active "
                    f"(state: {row.get('state')}).\n"
                    "Resuming here may conflict with the live session."
                ),
                handle_result,
            )
        else:
            proceed()

    def action_new_session(self) -> None:
        account, config_dir = self._focused_account()
        if not account:
            self.notify("Highlight an account or session first", severity="warning")
            return

        def handle_result(path: str | None) -> None:
            if not path:
                return
            error = spawn_new_terminal_window(path, config_dir or "", None)
            if error:
                self.notify(f"Couldn't open a new window: {error}", severity="error")
            else:
                self.notify(f"Opened a new session for {account} in a new Terminal window")

        self.push_screen(NewSessionScreen(account, default_dir=os.getcwd()), handle_result)


def main():
    Dashboard().run()


if __name__ == "__main__":
    main()
