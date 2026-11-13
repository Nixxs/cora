"""Hearing Claude Code's sessions (Settings > Claude Code): part of CoraApp (see app.py).

A listener of its own on 127.0.0.1 hears Cora's hooks in your Claude Code settings; what each session
is doing shows on the dashboard and in Settings, and noteworthy changes (finished, needs you) in
Activity. A session left finished or needing you for a few minutes, she tells you about.
"""

import asyncio

from textual import work

from claude_code import hooks
from claude_code.routes import DEFAULT_PORT, claude_code_routes
from claude_code.sessions import DEFAULT_TELL, NOTEWORTHY, TELL_CHOICES, SessionTracker, tell_text
from server.listener import Listener, build_app
from storage.webhook_settings_store import check_port
from app_areas.shared import WEBHOOK_STARTUP_SECONDS

HOOKS_SHOWN = {
    hooks.CURRENT: "Added to Claude Code's settings.",
    hooks.MISSING: "Not added to Claude Code's settings yet.",
    hooks.OUTDATED: "Claude Code's settings have an older version of Cora's hooks: add them again.",
}
# How often she looks for sessions left waiting long enough to tell you about.
TELL_CHECK_SECONDS = 15


class ClaudeCodeMixin:
    """Hearing Claude Code's sessions."""

    def init_claude_code(self) -> None:
        # Settings > Claude Code, as loaded at launch (off until then).
        self.claude_code_listen = False
        self.claude_code_port = DEFAULT_PORT
        self.claude_code_tell = DEFAULT_TELL
        self.claude_sessions = SessionTracker()
        self.claude_code_server: Listener | None = None
        self.claude_code_worker = None
        self.claude_code_token: str | None = None
        # What Settings shows: the listener's line, and the hooks' (state, line).
        self.claude_code_status = ""
        self.claude_code_hooks = ("", "")
        self.claude_code_lock = asyncio.Lock()
        self.set_interval(TELL_CHECK_SECONDS, self.tell_about_claude_code)

    def apply_claude_code_settings(self, saved: dict) -> None:
        """Settings > Claude Code, once at launch."""
        port = saved.get("port")
        self.claude_code_port = port if isinstance(port, int) else DEFAULT_PORT
        self.claude_code_tell = saved.get("tell") if saved.get("tell") in TELL_CHOICES else DEFAULT_TELL
        self.claude_code_listen = saved.get("listen") is True
        if self.claude_code_listen:
            self.restart_claude_code()

    @work(group="claude-code-setup", exit_on_error=False)
    async def restart_claude_code(self) -> None:
        await self.apply_claude_code()

    async def apply_claude_code(self) -> None:
        """Start, restart or stop the listener to match the settings; then look at the hooks."""
        async with self.claude_code_lock:
            await self.stop_claude_code()
            if self.closing or not self.claude_code_listen:
                return
            try:
                self.claude_code_token = await asyncio.to_thread(hooks.hook_token)
            except Exception as error:
                self.claude_code_status = "Not listening: the system credential store isn't available."
                self.activity.write(f"Claude Code's hook token couldn't be loaded ({type(error).__name__}).",
                                    "Claude Code")
                return
            port = self.claude_code_port
            server = Listener(build_app(claude_code_routes(lambda: self.claude_code_token,
                                                           self.on_claude_code_event)), port)
            try:
                server.open()
            except OSError as error:
                self.claude_code_status = f"Not listening: port {port} is in use or not allowed."
                self.activity.write(f"Claude Code's listener couldn't use port {port} "
                                    f"({error.strerror or 'in use'}).", "Claude Code")
                return
            self.claude_code_server = server
            self.claude_code_worker = self.run_worker(server.serve(), group="claude-code", exit_on_error=False)
            self.claude_code_status = f"Listening on 127.0.0.1:{port}"
            self.activity.write(f"Listening for Claude Code on 127.0.0.1:{port}.", "Claude Code")
        await self.check_claude_code_hooks()

    async def stop_claude_code(self) -> None:
        server, worker = self.claude_code_server, self.claude_code_worker
        self.claude_code_server = self.claude_code_worker = None
        self.claude_code_status = ""
        if server is None:
            return
        server.stop()
        if worker is not None:
            try:
                await asyncio.wait_for(worker.wait(), WEBHOOK_STARTUP_SECONDS)
            except Exception:
                worker.cancel()
        self.activity.write("Stopped listening for Claude Code.", "Claude Code")

    def on_claude_code_event(self, event: dict) -> None:
        session = self.claude_sessions.update(event)
        if session is not None and session.changed and session.state in NOTEWORTHY:
            self.activity.write(f"Claude Code · {session.line()}", "Claude Code")

    def tell_about_claude_code(self) -> None:
        """Sessions left finished or needing you a few minutes: one message, by voice or on Signal."""
        if self.closing or not self.claude_code_listen:
            return
        due = self.claude_sessions.due(TELL_CHOICES[self.claude_code_tell])
        if due:
            self.run_worker(self.tell_claude_code(due), group="tell-person", exit_on_error=False)

    async def tell_claude_code(self, sessions) -> None:
        projects = ", ".join(f"{session.project} ({session.state})" for session in sessions)
        try:
            await self.tell_person(tell_text(sessions, self.claude_sessions.clock()))
        except Exception as error:
            self.activity.write(f"Couldn't tell you about Claude Code in {projects} ({type(error).__name__}).",
                                "Claude Code")
            return
        self.activity.write(f"Telling you about Claude Code in {projects}.", "Claude Code")

    @work(group="model-settings-save")
    async def save_claude_code_tell(self, choice: str) -> None:
        if choice not in TELL_CHOICES:
            return
        self.claude_code_tell = choice
        try:
            await self.conversation_persistence.call_database(self.persistence.save_claude_code_tell, choice)
        except Exception as error:
            self.activity.write(f"Claude Code setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save what she tells you about; it applies until Cora closes.", severity="error")

    @work(group="claude-code-setup", exit_on_error=False)
    async def set_claude_code_listen(self, on: bool) -> None:
        self.claude_code_listen = on
        try:
            await self.conversation_persistence.call_database(self.persistence.save_claude_code_listen, on)
        except Exception as error:
            self.activity.write(f"Claude Code setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save listening for Claude Code; it applies until Cora closes.", severity="error")
        await self.apply_claude_code()

    @work(group="claude-code-setup", exit_on_error=False)
    async def save_claude_code_port(self, port: str) -> None:
        try:
            port_number = check_port(int(port.strip() or "0"))
        except ValueError as error:
            self.notify(str(error) if "between" in str(error) else "The port must be a number.", severity="warning")
            return
        if port_number == self.webhook_settings.port:
            self.notify("That's the webhooks' port; Claude Code needs one of its own.", severity="warning")
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.save_claude_code_port, port_number)
        except Exception as error:
            self.activity.write(f"Claude Code setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        self.claude_code_port = port_number
        self.notify("Port saved. Add the hooks again so Claude Code uses it.")
        await self.apply_claude_code()

    @work(group="claude-code-hooks", exclusive=True, exit_on_error=False)
    async def add_claude_code_hooks(self) -> None:
        def add() -> None:
            token = hooks.hook_token()
            hooks.write_settings(hooks.with_cora(hooks.read_settings(), self.claude_code_port, token))

        await self.change_claude_code_hooks(add, "Cora's hooks added to Claude Code's settings.")

    @work(group="claude-code-hooks", exclusive=True, exit_on_error=False)
    async def remove_claude_code_hooks(self) -> None:
        def remove() -> None:
            settings = hooks.read_settings()
            if hooks.without_cora(settings) != settings:
                hooks.write_settings(hooks.without_cora(settings))

        await self.change_claude_code_hooks(remove, "Cora's hooks removed from Claude Code's settings.")

    async def change_claude_code_hooks(self, change, done: str) -> None:
        try:
            await asyncio.to_thread(change)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Changing Claude Code's settings failed ({type(error).__name__}).", "Claude Code")
            self.notify("Claude Code's settings couldn't be changed. Check Activity.", severity="error")
            return
        self.activity.write(f"{done} A copy of the file as it was is beside it.", "Claude Code")
        self.notify(done)
        await self.check_claude_code_hooks()

    @work(group="claude-code-check", exclusive=True, exit_on_error=False)
    async def refresh_claude_code_hooks(self) -> None:
        await self.check_claude_code_hooks()

    async def check_claude_code_hooks(self) -> None:
        """What Settings says about the hooks, from the settings file as it is now."""
        def state() -> str:
            return hooks.hooks_state(hooks.read_settings(), self.claude_code_port, hooks.hook_token())

        try:
            found = await asyncio.to_thread(state)
        except ValueError as error:
            self.claude_code_hooks = ("error", str(error))
            return
        except Exception as error:
            self.claude_code_hooks = ("error", f"Claude Code's settings couldn't be read ({type(error).__name__}).")
            return
        self.claude_code_hooks = (found, HOOKS_SHOWN[found])
