"""Hearing Claude Code's sessions: the hooks Cora adds to its settings, the route they post to, and
what each session is doing."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from app_areas.claude_code import ClaudeCodeMixin
from app_areas.web_interface import WebInterfaceMixin
from claude_code import hooks, transcripts
from tools.claude_code import ClaudeCodeTool, validate_arguments as validate_tool_arguments
from claude_code.routes import BODY_MAX_BYTES, EVENT_PATH, PERMISSION_PATH, claude_code_routes, permission_decision
from claude_code.sessions import (
    ENDED, FAILED, FINISHED, NEEDS_YOU, SESSIONS_MAX, STARTED, TELL_AFTER, TELL_CHOICES, WORKING, SessionTracker,
    project_name, tell_text,
)
from server.listener import build_app
from ui.claude_code_view import claude_code_lines, follow_text
from claude_code.transcripts import recent_exchanges, session_transcript

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)

TOKEN = "abcDEF123_-tokentokentokentokentokentoken"
PORT = 8001


def event(name: str, session_id: str = "s1", cwd: str = r"D:\Development\cora", **fields) -> dict:
    return {"hook_event_name": name, "session_id": session_id, "cwd": cwd, **fields}


class SessionTrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
        self.tracker = SessionTracker(clock=lambda: self.now)

    def test_a_session_follows_what_its_hooks_say(self):
        self.assertEqual(self.tracker.update(event("UserPromptSubmit", prompt="fix the tests")).state, WORKING)
        waiting = self.tracker.update(event("Notification", notification_type="permission_prompt",
                                            message="Claude needs your permission to use Bash"))
        self.assertEqual((waiting.state, waiting.line()),
                         (NEEDS_YOU, "cora: needs you (Claude needs your permission to use Bash)"))
        finished = self.tracker.update(event("Stop", last_assistant_message="All  tests\npass."))
        self.assertEqual((finished.state, finished.detail, finished.line()), (FINISHED, "All tests pass.", "cora: finished"))
        self.assertEqual(self.tracker.update(event("SessionEnd", reason="other")).state, ENDED)
        self.assertEqual(self.tracker.update(event("StopFailure", error="rate_limit")).state, FAILED)

    def test_events_that_say_nothing_new_are_ignored(self):
        self.assertIsNone(self.tracker.update(event("Notification", notification_type="idle_prompt")))
        self.assertIsNone(self.tracker.update(event("PreToolUse")))
        self.assertIsNone(self.tracker.update(event("Stop", session_id="")))
        self.assertIsNone(self.tracker.update(event("Stop", session_id=7)))
        self.assertIsNone(self.tracker.update("Stop"))
        self.assertEqual(self.tracker.sessions, {})

    def test_long_text_is_cut_to_one_short_line(self):
        session = self.tracker.update(event("Stop", last_assistant_message="word " * 500))
        self.assertLessEqual(len(session.detail), 300)
        self.assertTrue(session.detail.endswith("…"))
        self.assertNotIn("\n", session.detail)

    def test_a_missing_folder_keeps_the_one_known(self):
        self.tracker.update(event("UserPromptSubmit"))
        self.assertEqual(self.tracker.update(event("Stop", cwd=None)).project, "cora")

    def test_the_longest_quiet_sessions_go_first_and_the_newest_shows_first(self):
        for number in range(SESSIONS_MAX + 1):
            self.now += timedelta(seconds=1)
            self.tracker.update(event("Stop", session_id=f"s{number}"))
        recent = self.tracker.recent()
        self.assertEqual(len(recent), SESSIONS_MAX)
        self.assertEqual(recent[0].session_id, f"s{SESSIONS_MAX}")
        self.assertNotIn("s0", self.tracker.sessions)

    def test_carrying_on_after_you_answer_clears_needs_you_and_keeps_the_prompt(self):
        self.tracker.update(event("UserPromptSubmit", prompt="fix the tests"))
        self.tracker.update(event("Notification", notification_type="permission_prompt", message="Allow Bash?"))
        self.now += timedelta(minutes=1)
        carrying_on = self.tracker.update(event("PostToolUse", tool_name="Bash", tool_response="lots"))
        self.assertEqual((carrying_on.state, carrying_on.detail, carrying_on.changed), (WORKING, "fix the tests", True))
        self.now += timedelta(minutes=1)
        again = self.tracker.update(event("PostToolUse", tool_name="Read"))
        # Still the same stretch of work: since doesn't move, and Activity hears nothing new.
        self.assertEqual((again.since, again.changed), (carrying_on.since, False))

    def test_compacting_doesnt_restart_a_session(self):
        self.tracker.update(event("UserPromptSubmit", prompt="go"))
        self.assertIsNone(self.tracker.update(event("SessionStart", source="compact")))
        self.assertEqual(self.tracker.update(event("SessionStart", source="resume")).state, STARTED)

    def test_the_project_is_the_folder_name_on_any_system(self):
        self.assertEqual(project_name(r"D:\Development\cora"), "cora")
        self.assertEqual(project_name("/home/me/site/"), "site")


class TellingYouTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
        self.tracker = SessionTracker(clock=lambda: self.now)

    def test_a_session_left_waiting_comes_up_once_after_the_wait(self):
        self.tracker.update(event("Notification", notification_type="permission_prompt", message="Allow Bash?"))
        self.now += TELL_AFTER - timedelta(seconds=1)
        self.assertEqual(self.tracker.due(TELL_CHOICES["all"]), [])
        self.now += timedelta(seconds=1)
        self.assertEqual([session.state for session in self.tracker.due(TELL_CHOICES["all"])], [NEEDS_YOU])
        self.now += TELL_AFTER
        self.assertEqual(self.tracker.due(TELL_CHOICES["all"]), [])

    def test_answering_in_time_means_nothing_is_said_and_a_new_wait_starts_afresh(self):
        self.tracker.update(event("Notification", notification_type="permission_prompt"))
        self.now += timedelta(minutes=1)
        self.tracker.update(event("PostToolUse"))
        self.now += TELL_AFTER
        self.assertEqual(self.tracker.due(TELL_CHOICES["all"]), [])
        self.tracker.update(event("Stop", last_assistant_message="Done."))
        self.now += TELL_AFTER
        self.assertEqual([session.state for session in self.tracker.due(TELL_CHOICES["all"])], [FINISHED])

    def test_what_you_chose_decides_what_comes_up_and_old_ones_never_do(self):
        self.tracker.update(event("Stop", session_id="done"))
        self.tracker.update(event("StopFailure", session_id="broke", error="rate_limit"))
        self.now += TELL_AFTER
        self.assertEqual([session.session_id for session in self.tracker.due(TELL_CHOICES["needs"])], ["broke"])
        # Switching to everything later doesn't bring up the finished one it let pass.
        self.assertEqual(self.tracker.due(TELL_CHOICES["all"]), [])
        self.tracker.update(event("Stop", session_id="later"))
        self.now += TELL_AFTER
        self.assertEqual(self.tracker.due(TELL_CHOICES["off"]), [])

    def test_what_she_says_names_the_project_the_wait_and_the_detail(self):
        self.tracker.update(event("Notification", notification_type="permission_prompt", message="Allow Bash?"))
        self.tracker.update(event("Stop", session_id="s2", cwd="/home/me/site"))
        self.now += timedelta(minutes=4)
        text = tell_text(self.tracker.due(TELL_CHOICES["all"]), self.now)
        self.assertEqual(text, "Claude Code in cora has been waiting on you for 4 minutes: Allow Bash?\n"
                               "Claude Code finished in site 4 minutes ago.")

    def test_the_dashboard_lists_what_needs_you_first_and_drops_old_ended_sessions(self):
        self.tracker.update(event("SessionEnd", session_id="gone", cwd="/old", reason="other"))
        self.now += timedelta(minutes=20)
        self.tracker.update(event("Stop", session_id="done", cwd="/done", last_assistant_message="All good."))
        self.tracker.update(event("Notification", session_id="wait", cwd="/wait",
                                  notification_type="permission_prompt", message="Allow Bash?"))
        shown = claude_code_lines(self.tracker.recent(), self.now).plain
        self.assertLess(shown.index("wait  needs you"), shown.index("done  finished"))
        self.assertIn("Allow Bash?", shown)
        self.assertNotIn("old", shown)
        self.assertEqual(claude_code_lines([], self.now).plain, "no sessions yet")


def transcript_line(kind: str, content, **fields) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **fields})


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.projects = Path(folder.name) / "projects"
        (self.projects / "d--cora").mkdir(parents=True)
        self.path = self.projects / "d--cora" / "s1.jsonl"

    def write(self, *lines: str) -> str:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(self.path)

    def test_your_words_claudes_replies_and_its_tools_but_never_thinking_or_output(self):
        path = self.write(
            transcript_line("user", "fix the failing test"),
            transcript_line("assistant", [{"type": "thinking", "thinking": "secret plan"},
                                          {"type": "text", "text": "Looking at it."}]),
            transcript_line("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]),
            transcript_line("user", [{"type": "tool_result", "content": "1 failed"}]),
            transcript_line("assistant", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "app.py"}}]),
            transcript_line("user", [{"type": "text", "text": "<system-reminder>ignore</system-reminder>"}]),
            transcript_line("assistant", [{"type": "text", "text": "Fixed: the test passes."}]),
            transcript_line("assistant", [{"type": "text", "text": "a helper's work"}], isSidechain=True),
            json.dumps({"type": "api-request-blob", "message": {"role": "user", "content": "raw request"}}),
            "not json",
        )
        self.assertEqual(transcripts.recent_exchanges(path, self.projects), [
            {"who": "you", "said": "fix the failing test"},
            {"who": "claude", "said": "Looking at it."},
            {"who": "claude", "said": "used Edit: app.py (after 1 other tool use)"},
            {"who": "claude", "said": "Fixed: the test passes."},
        ])

    def test_only_the_newest_fit(self):
        path = self.write(*(transcript_line("user", f"message {number}") for number in range(40)))
        exchanges = transcripts.recent_exchanges(path, self.projects)
        self.assertEqual(len(exchanges), transcripts.EXCHANGES_MAX)
        self.assertEqual(exchanges[-1]["said"], "message 39")
        long = self.write(*(transcript_line("user", "word " * 400) for _ in range(40)))
        exchanges = transcripts.recent_exchanges(long, self.projects)
        self.assertLessEqual(sum(len(item["said"]) for item in exchanges), transcripts.TOTAL_MAX_CHARS)

    def test_only_transcripts_in_claude_codes_projects_folder_are_read(self):
        outside = Path(self.projects.parent) / "notes.jsonl"
        outside.write_text(transcript_line("user", "private"), encoding="utf-8")
        not_jsonl = self.projects / "d--cora" / "notes.txt"
        not_jsonl.write_text(transcript_line("user", "private"), encoding="utf-8")
        sneaky = str(self.projects / "d--cora" / ".." / ".." / "notes.jsonl")
        for path in (str(outside), str(not_jsonl), sneaky, str(self.projects / "missing.jsonl"), ""):
            self.assertIsNone(transcripts.recent_exchanges(path, self.projects), path)


class ClaudeCodeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
        self.app = SimpleNamespace(
            claude_code_listen=True, claude_code_transcripts=False,
            claude_sessions=SessionTracker(clock=lambda: self.now), activity=MagicMock(),
        )

    async def look(self, project=None, recent=False) -> dict:
        return await ClaudeCodeMixin.look_at_claude_code(self.app, project, recent)

    def test_its_arguments(self):
        validate_tool_arguments({"project": None, "recent_exchanges": False})
        validate_tool_arguments({"project": "cora", "recent_exchanges": True})
        for bad in ({}, {"project": None}, {"project": 7, "recent_exchanges": False},
                    {"project": None, "recent_exchanges": "yes"},
                    {"project": "x" * 201, "recent_exchanges": False},
                    {"project": None, "recent_exchanges": False, "more": 1}):
            with self.assertRaises(ValueError):
                validate_tool_arguments(bad)

    def test_only_offered_while_listening(self):
        app = SimpleNamespace(maps_key="key", calling_out_problem=lambda: None, claude_code_listen=False,
                              can_run_claude_code=lambda: False)
        self.assertIn("claude_code", WebInterfaceMixin.unavailable_tools(app))
        app.claude_code_listen = True
        self.assertNotIn("claude_code", WebInterfaceMixin.unavailable_tools(app))

    async def test_not_listening_says_how_to_turn_it_on(self):
        self.app.claude_code_listen = False
        self.assertIn("Settings > Claude Code", (await self.look())["message"])

    async def test_every_session_or_one_project_with_its_state_and_detail(self):
        self.app.claude_sessions.update(event("UserPromptSubmit", prompt="fix the tests"))
        self.app.claude_sessions.update(event("Notification", notification_type="permission_prompt",
                                              message="Allow Bash?"))
        self.app.claude_sessions.update(event("Stop", session_id="s2", cwd="/home/me/site",
                                              last_assistant_message="Deployed."))
        self.now += timedelta(minutes=4)
        everything = await self.look()
        self.assertEqual([found["project"] for found in everything["sessions"]], ["cora", "site"])
        self.assertEqual(everything["sessions"][0], {
            "project": "cora", "folder": r"D:\Development\cora", "state": NEEDS_YOU, "for": "4 minutes",
            "last_heard": "4 minutes ago", "detail": "Allow Bash?", "last_prompt": "fix the tests"})
        self.assertEqual([found["project"] for found in (await self.look("SIT"))["sessions"]], ["site"])
        missing = await self.look("rocky")
        self.assertEqual(missing["sessions"], [])
        self.assertIn("cora, site", missing["message"])

    async def test_transcripts_only_when_allowed(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "s1.jsonl"
        path.write_text(transcript_line("user", "fix the tests"), encoding="utf-8")
        self.app.claude_sessions.update(event("UserPromptSubmit", prompt="fix the tests", transcript_path=str(path)))
        self.assertIn("hasn't let you", (await self.look("cora", recent=True))["recent_exchanges"])
        self.app.claude_code_transcripts = True
        # Read from this folder in place of Claude Code's own projects folder.
        with patch.object(transcripts.recent_exchanges, "__defaults__",
                          (Path(folder.name), *transcripts.recent_exchanges.__defaults__[1:])):
            read = await self.look("cora", recent=True)
        self.assertEqual(read["recent_exchanges"],
                         {"project": "cora", "exchanges": [{"who": "you", "said": "fix the tests"}]})
        self.assertIn("transcript", self.app.activity.write.call_args.args[0])

    async def test_the_tool_sends_the_result_back_and_she_carries_on(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        look = AsyncMock(return_value={"status": "ok", "sessions": []})
        tool = ClaudeCodeTool(lambda: connection, lambda: False, look, MagicMock())
        await tool.handle("call-1", {"project": "  ", "recent_exchanges": False})
        look.assert_awaited_once_with(None, False)
        sent = connection.response.item.create.await_args.kwargs["item"]
        self.assertEqual((sent["call_id"], json.loads(sent["output"])["status"]), ("call-1", "ok"))
        connection.response.create.assert_awaited_once()


class FollowingASessionTests(unittest.TestCase):
    """The pop-up that follows a session: found by its path, or by its id for sessions Cora runs."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.projects = Path(folder.name)
        (self.projects / "D--Games-ComboDots").mkdir()
        self.transcript = self.projects / "D--Games-ComboDots" / "abc-123.jsonl"
        lines = [{"type": "user", "message": {"content": "make five dots " + "x" * 900}}]
        lines += [{"type": "assistant", "message": {"content": [{"type": "text", "text": f"step {n}"}]}}
                  for n in range(30)]
        self.transcript.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")

    def test_a_transcript_is_found_by_its_path_or_its_session_id(self):
        self.assertEqual(session_transcript("abc-123", "given.jsonl", self.projects), "given.jsonl")
        self.assertEqual(session_transcript("abc-123", "", self.projects), str(self.transcript))
        self.assertIsNone(session_transcript("missing", "", self.projects))
        self.assertIsNone(session_transcript("../abc-123", "", self.projects))

    def test_the_pop_up_shows_more_than_cora_reads(self):
        hers = recent_exchanges(str(self.transcript), self.projects)
        yours = recent_exchanges(str(self.transcript), self.projects, most=40, total_max=30000, entry_max=2000)
        self.assertEqual(len(hers), 12)
        self.assertEqual(len(yours), 31)
        self.assertGreater(len(yours[0]["said"]), 900)

    def test_its_text_says_where_how_long_and_what_was_said(self):
        tracker = SessionTracker(clock=lambda: NOW)
        session = tracker.update({"hook_event_name": "UserPromptSubmit", "session_id": "abc-123",
                                  "cwd": "D:\\Games\\ComboDots", "prompt": "make five dots"})
        text = follow_text(session, NOW, [{"who": "you", "said": "make five dots"},
                                          {"who": "claude", "said": "done"}])
        self.assertTrue(text.startswith("D:\\Games\\ComboDots\nworking for under a minute"))
        self.assertIn("Asked: make five dots", text)
        self.assertTrue(text.rstrip().endswith("Claude: done"))
        self.assertIn("can't be read", follow_text(session, NOW, None))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.heard = []
        self.token = TOKEN
        self.client = TestClient(build_app(claude_code_routes(lambda: self.token, self.heard.append)))

    def post(self, body, token: str | None = TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(EVENT_PATH, content=body, headers=headers)

    def test_an_event_with_the_token_is_heard_and_answered_with_nothing(self):
        response = self.post(json.dumps(event("Stop")))
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(self.heard, [event("Stop")])

    def test_without_the_right_token_nothing_is_heard(self):
        for token in (None, "wrong", TOKEN + "x"):
            self.assertEqual(self.post(json.dumps(event("Stop")), token).status_code, 401)
        self.token = None
        self.assertEqual(self.post(json.dumps(event("Stop"))).status_code, 401)
        self.assertEqual(self.heard, [])

    def test_bad_or_oversized_bodies_are_refused(self):
        self.assertEqual(self.post("not json").status_code, 400)
        self.assertEqual(self.post("[1, 2]").status_code, 400)
        self.assertEqual(self.post(b"x" * (BODY_MAX_BYTES + 1)).status_code, 413)
        self.assertEqual(self.heard, [])

    def test_only_posts_reach_it(self):
        self.assertEqual(self.client.get(EVENT_PATH).status_code, 405)

    def test_a_permission_request_gets_her_decision_or_nothing(self):
        decisions = []

        async def on_permission(heard):
            decisions.append(heard)
            return permission_decision(heard["tool_name"] == "Bash", "no")

        client = TestClient(build_app(claude_code_routes(lambda: self.token, self.heard.append, on_permission)))
        headers = {"Authorization": f"Bearer {TOKEN}"}
        allowed = client.post(PERMISSION_PATH, json={"tool_name": "Bash"}, headers=headers)
        self.assertEqual(allowed.json()["hookSpecificOutput"],
                         {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}})
        denied = client.post(PERMISSION_PATH, json={"tool_name": "WebFetch"}, headers=headers).json()
        self.assertEqual(denied["hookSpecificOutput"]["decision"], {"behavior": "deny", "message": "no"})
        self.assertEqual(client.post(PERMISSION_PATH, json={"tool_name": "Bash"}).status_code, 401)
        self.assertEqual(len(decisions), 2)
        # Without a handler: nothing, so Claude Code asks itself.
        none = self.client.post(PERMISSION_PATH, json={"tool_name": "Bash"}, headers=headers)
        self.assertEqual((none.status_code, none.content), (204, b""))
        self.assertEqual(self.heard, [])


class HookSettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / ".claude" / "settings.json"

    def test_cora_adds_one_background_hook_per_event_and_keeps_everything_else(self):
        theirs = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
        settings = hooks.with_cora(theirs, PORT, TOKEN)
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["hooks"]["Stop"][0], theirs["hooks"]["Stop"][0])
        for name in hooks.EVENTS:
            cora = [hook for group in settings["hooks"][name] for hook in group["hooks"] if hooks.is_cora_hook(hook)]
            self.assertEqual(len(cora), 1)
            self.assertTrue(cora[0]["async"])
            self.assertIn(f"Bearer {TOKEN}", cora[0]["command"])
            self.assertIn(f"http://127.0.0.1:{PORT}{EVENT_PATH}", cora[0]["command"])
        self.assertEqual(hooks.hooks_state(settings, PORT, TOKEN), hooks.CURRENT)
        # Theirs untouched by adding.
        self.assertEqual(len(theirs["hooks"]["Stop"]), 1)

    def test_the_permission_hook_waits_for_her_and_never_blocks_when_she_is_closed(self):
        settings = hooks.with_cora({}, PORT, TOKEN)
        [group] = settings["hooks"]["PermissionRequest"]
        [hook] = group["hooks"]
        self.assertNotIn("async", hook)
        self.assertEqual(hook["onFailure"], "continue")
        self.assertIn(f"http://127.0.0.1:{PORT}{PERMISSION_PATH}", hook["command"])
        self.assertGreater(hook["timeout"], 600)
        self.assertEqual(hooks.without_cora(settings), {})

    def test_hooks_added_before_the_permission_hook_are_out_of_date(self):
        settings = hooks.with_cora({}, PORT, TOKEN)
        del settings["hooks"]["PermissionRequest"]
        self.assertEqual(hooks.hooks_state(settings, PORT, TOKEN), hooks.OUTDATED)

    def test_adding_again_replaces_corass_hooks_rather_than_doubling_them(self):
        settings = hooks.with_cora(hooks.with_cora({}, PORT, "old"), PORT + 1, TOKEN)
        self.assertEqual(hooks.hooks_state(settings, PORT + 1, TOKEN), hooks.CURRENT)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)

    def test_removing_takes_out_only_coras_hooks(self):
        theirs = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
        self.assertEqual(hooks.without_cora(hooks.with_cora(theirs, PORT, TOKEN)), theirs)
        self.assertEqual(hooks.without_cora(hooks.with_cora({"model": "opus"}, PORT, TOKEN)), {"model": "opus"})

    def test_a_different_port_or_token_or_a_missing_event_is_out_of_date(self):
        settings = hooks.with_cora({}, PORT, TOKEN)
        self.assertEqual(hooks.hooks_state(settings, PORT + 1, TOKEN), hooks.OUTDATED)
        self.assertEqual(hooks.hooks_state(settings, PORT, "other"), hooks.OUTDATED)
        del settings["hooks"]["SessionEnd"]
        self.assertEqual(hooks.hooks_state(settings, PORT, TOKEN), hooks.OUTDATED)
        self.assertEqual(hooks.hooks_state({}, PORT, TOKEN), hooks.MISSING)

    def test_the_file_is_written_whole_with_a_copy_of_how_it_was(self):
        self.assertEqual(hooks.read_settings(self.path), {})
        hooks.write_settings({"model": "opus"}, self.path)
        self.assertFalse(self.path.with_name("settings.json.cora-backup").exists())
        hooks.write_settings(hooks.with_cora(hooks.read_settings(self.path), PORT, TOKEN), self.path)
        self.assertEqual(json.loads(self.path.with_name("settings.json.cora-backup").read_text()), {"model": "opus"})
        self.assertEqual(hooks.hooks_state(hooks.read_settings(self.path), PORT, TOKEN), hooks.CURRENT)

    def test_a_file_that_cant_be_read_safely_is_left_alone(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", "[1]", '{"hooks": []}'):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                hooks.read_settings(self.path)


if __name__ == "__main__":
    unittest.main()
