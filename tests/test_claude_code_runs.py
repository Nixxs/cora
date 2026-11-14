"""Cora running Claude Code herself: projects folders, the runner (with a stand-in for the SDK's
client), what each step asks of you, and her run_claude_code tool."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import AssistantMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, \
    SystemMessage, TextBlock

from app_areas.claude_code import ClaudeCodeMixin
from app_areas.web_interface import WebInterfaceMixin
from claude_code import runner as runner_module
from claude_code.runner import ClaudeCodeRunner, check_folders, find_project, new_project, parse_folders
from claude_code.sessions import FINISHED, SessionTracker
from services.confirmation import APPROVED, DECLINED, TIMED_OUT, Confirmations
from tools.run_claude_code import RunClaudeCodeTool, validate_arguments


class FakeClient:
    """Plays back messages like ClaudeSDKClient, or raises part way."""

    def __init__(self, options, messages, fail=None, hang=False):
        self.options = options
        self.messages = messages
        self.fail = fail
        # Waits after its messages until interrupted, then ends the turn as Claude Code does.
        self.hang = hang
        self.released = asyncio.Event()
        self.queries = []
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            await asyncio.sleep(0)
            yield message
        if self.fail is not None:
            raise self.fail
        if self.hang:
            await self.released.wait()
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
                                num_turns=1, session_id="s1")

    async def interrupt(self):
        self.interrupted = True
        self.released.set()


def played(session_id="s1", result="Built it.", is_error=False):
    return [
        SystemMessage(subtype="init", data={"session_id": session_id}),
        AssistantMessage(content=[TextBlock(text="Working on it.")], model="claude"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                      session_id=session_id, result=result),
    ]


class FolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "Development" / "cora").mkdir(parents=True)
        (self.root / "AddOns").mkdir()
        self.folders = [str(self.root / "Development"), str(self.root / "AddOns")]

    def test_folders_as_typed(self):
        self.assertEqual(parse_folders(' D:\\Dev ; "E:\\AddOns";d:\\dev\n'), ["D:\\Dev", "E:\\AddOns"])
        self.assertEqual(check_folders(self.folders), self.folders)
        for bad in (["relative\\path"], [str(self.root / "missing")]):
            with self.assertRaises(ValueError):
                check_folders(bad)

    def test_existing_projects_are_found_by_name_in_any_folder(self):
        self.assertEqual(find_project(self.folders, "CORA"), self.root / "Development" / "cora")
        self.assertIsNone(find_project(self.folders, "rocky"))

    def test_a_new_project_goes_in_the_first_folder_or_the_one_named(self):
        self.assertEqual(new_project(self.folders, "site", None), self.root / "Development" / "site")
        self.assertEqual(new_project(self.folders, "MountTracker", "addons"), self.root / "AddOns" / "MountTracker")
        for name, location in (("cora", None), ("../escape", None), ("a/b", None), ("..", None), ("x", "Games")):
            with self.assertRaises(ValueError):
                new_project(self.folders, name, location)
        with self.assertRaises(ValueError):
            new_project([], "site", None)

    def test_an_api_key_in_the_environment_stops_it(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-x"}):
            self.assertIn("ANTHROPIC_API_KEY", runner_module.subscription_problem())
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, clear=True):
            self.assertIsNone(runner_module.subscription_problem())


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "site"
        self.events, self.done, self.asked = [], asyncio.Event(), []
        self.outcome = {}
        self.approve = APPROVED
        self.messages, self.fail, self.hang = played(), None, False
        self.clients = []

        async def ask(project, tool, shown):
            self.asked.append((project, tool, shown))
            return self.approve

        async def on_done(run, state, text):
            self.outcome.update(state=state, text=text, run=run)
            self.done.set()

        def client(options):
            made = FakeClient(options, self.messages, self.fail, self.hang)
            self.clients.append(made)
            return made

        self.runner = ClaudeCodeRunner(
            ask_approval=ask, on_event=self.events.append, on_done=on_done, write_activity=MagicMock(),
            schedule=lambda run, context: asyncio.get_running_loop().create_task(run, context=context),
            client_factory=client, options_factory=lambda **options: SimpleNamespace(**options),
        )

    async def finished(self):
        await asyncio.wait_for(self.done.wait(), 2)
        return self.outcome

    async def test_a_session_runs_in_its_folder_and_reports_like_the_hooks(self):
        self.runner.start("site", self.folder, "make a site")
        self.assertTrue(self.runner.busy)
        outcome = await self.finished()
        self.assertEqual((outcome["state"], outcome["text"]), ("finished", "Built it."))
        self.assertTrue(self.folder.is_dir())
        self.assertFalse(self.runner.busy)
        options = self.clients[0].options
        self.assertEqual((options.cwd, options.permission_mode, options.resume), (str(self.folder), "auto", None))
        self.assertIn("user", options.setting_sources)
        self.assertEqual(self.clients[0].queries, ["make a site"])
        self.assertEqual([(event["hook_event_name"], event["session_id"]) for event in self.events],
                         [("UserPromptSubmit", "s1"), ("Stop", "s1")])
        # What the tracker makes of them.
        tracker = SessionTracker(clock=lambda: datetime(2026, 11, 13, tzinfo=timezone.utc))
        for event in self.events:
            tracker.update(event)
        self.assertEqual((tracker.sessions["s1"].state, tracker.sessions["s1"].project), (FINISHED, "site"))
        self.assertEqual(self.runner.sessions[str(self.folder).casefold()], "s1")

    async def test_one_at_a_time(self):
        self.runner.start("site", self.folder, "make a site")
        with self.assertRaises(RuntimeError):
            self.runner.start("other", self.folder, "more")
        await self.finished()

    async def test_continuing_resumes_and_a_persons_session_is_forked(self):
        self.runner.start("site", self.folder, "add a page", resume="theirs", fork=True)
        await self.finished()
        options = self.clients[0].options
        self.assertEqual((options.resume, options.fork_session), ("theirs", True))

    async def test_an_error_or_a_failed_result_is_failed(self):
        self.messages = played(is_error=True, result="Out of usage.")
        self.runner.start("site", self.folder, "make a site")
        self.assertEqual(((await self.finished())["state"], self.outcome["text"]), ("failed", "Out of usage."))
        self.assertEqual(self.events[-1]["hook_event_name"], "StopFailure")
        self.done.clear()
        self.messages, self.fail = played()[:1], RuntimeError("the CLI went away")
        self.runner.start("site", self.folder, "again")
        outcome = await self.finished()
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("RuntimeError", outcome["text"])

    async def test_stopping_interrupts_it(self):
        self.messages, self.hang = played()[:2], True
        self.runner.start("site", self.folder, "make a site")
        await asyncio.sleep(0.05)
        self.assertTrue(await self.runner.stop())
        self.assertTrue(self.clients[0].interrupted)
        outcome = await self.finished()
        self.assertEqual((outcome["state"], outcome["text"]), ("stopped", "Working on it."))
        self.assertFalse(await self.runner.stop())

    async def test_anything_beyond_its_files_is_asked_and_questions_wait_for_its_summary(self):
        run = SimpleNamespace(project="site")
        permit = self.runner.permit_for(run)
        allowed = await permit("Bash", {"command": "npm test", "description": "x" * 1000}, None)
        self.assertIsInstance(allowed, PermissionResultAllow)
        self.assertEqual(self.asked[0][:2], ("site", "Bash"))
        self.assertLessEqual(len(self.asked[0][2]["description"]), 300)
        self.approve = DECLINED
        declined = await permit("WebFetch", {"url": "https://x"}, None)
        self.assertIsInstance(declined, PermissionResultDeny)
        self.assertIn("didn't approve", declined.message)
        self.approve = TIMED_OUT
        self.assertIn("didn't answer in time", (await permit("WebFetch", {"url": "https://x"}, None)).message)
        asked = len(self.asked)
        question = await permit("AskUserQuestion", {"questions": []}, None)
        self.assertIsInstance(question, PermissionResultDeny)
        self.assertEqual(len(self.asked), asked)


class ApprovalsWhereYouAreTests(unittest.IsolatedAsyncioTestCase):
    """A session's steps are asked at the desk while she's active, and on Signal while she's in standby."""

    def setUp(self):
        self.shown, self.on_signal = [], []

        async def ask_on_signal(request):
            self.on_signal.append(request)
            return True

        self.app = SimpleNamespace(
            standby=False, closing=False, signal_reaches_person=lambda: True,
            signal=SimpleNamespace(status=SimpleNamespace(account="+61400000000")),
            signal_settings=SimpleNamespace(person_recipient="+61411111111", allowed_number="+61411111111"),
            signal_approval_asker=lambda account, person, number: ask_on_signal,
            confirmations=Confirmations(show=self.shown.append, close=MagicMock(), write_activity=MagicMock(),
                                        timeout=0.2),
        )
        self.app.claude_code_approval_route = lambda: ClaudeCodeMixin.claude_code_approval_route(self.app)

    async def ask(self):
        return await ClaudeCodeMixin.approve_claude_code_step(self.app, "site", "Bash", {"command": "npm test"})

    async def test_at_the_desk_while_shes_active(self):
        self.assertEqual(await self.ask(), TIMED_OUT)
        self.assertEqual([request.title for request in self.shown], ["Claude Code · site"])
        self.assertEqual(self.on_signal, [])

    async def test_on_signal_while_shes_in_standby(self):
        self.app.standby = True
        self.assertEqual(await self.ask(), APPROVED)
        self.assertEqual(self.shown, [])
        self.assertEqual((self.on_signal[0].tool, self.on_signal[0].arguments), ("Bash", {"command": "npm test"}))

    async def test_at_the_desk_without_signal(self):
        self.app.standby = True
        self.app.signal_reaches_person = lambda: False
        self.assertEqual(await self.ask(), TIMED_OUT)
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.on_signal, [])


class YourSessionsPermissionTests(unittest.IsolatedAsyncioTestCase):
    """Your own sessions' permission prompts: answered on Signal while she's in standby, else left to Claude Code."""

    def setUp(self):
        self.on_signal, self.answer = [], True

        async def ask_on_signal(request):
            self.on_signal.append(request)
            if self.answer is None:
                await asyncio.Event().wait()
            return self.answer

        self.app = SimpleNamespace(
            standby=True, closing=False, signal_reaches_person=lambda: True,
            signal=SimpleNamespace(status=SimpleNamespace(account="+61400000000")),
            signal_settings=SimpleNamespace(person_recipient="+61411111111", allowed_number="+61411111111"),
            signal_approval_asker=lambda account, person, number: ask_on_signal,
            confirmations=Confirmations(show=MagicMock(), close=MagicMock(), write_activity=MagicMock()),
            claude_sessions=SessionTracker(), activity=MagicMock(),
            claude_code_runner=SimpleNamespace(current=None, sessions={}),
        )
        for name in ("claude_code_approval_route", "is_her_claude_code_session", "on_claude_code_permission"):
            setattr(self.app, name, getattr(ClaudeCodeMixin, name).__get__(self.app))
        patcher = patch("app_areas.claude_code.PERMISSION_CHECK_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, session_id="vscode", tool="Bash"):
        return {"hook_event_name": "PermissionRequest", "session_id": session_id, "cwd": r"D:\Development\cora",
                "tool_name": tool, "tool_input": {"command": "npm test"}}

    async def test_while_shes_active_claude_code_asks_you_itself(self):
        self.app.standby = False
        self.assertIsNone(await self.app.on_claude_code_permission(self.request()))
        self.assertEqual(self.on_signal, [])
        self.assertEqual(self.app.claude_sessions.sessions, {})

    async def test_in_standby_your_answer_on_signal_goes_to_claude_code(self):
        allowed = await self.app.on_claude_code_permission(self.request())
        self.assertEqual(allowed["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        self.assertEqual((self.on_signal[0].title, self.on_signal[0].tool), ("Claude Code · cora", "Bash"))
        self.assertEqual(self.on_signal[0].arguments, {"command": "npm test"})
        session = self.app.claude_sessions.sessions["vscode"]
        self.assertEqual(session.state, "needs you")
        self.assertTrue(session.told)
        self.answer = False
        denied = await self.app.on_claude_code_permission(self.request())
        self.assertEqual(denied["hookSpecificOutput"]["decision"]["behavior"], "deny")

    async def test_her_own_sessions_and_questions_are_left_alone(self):
        self.app.claude_code_runner.sessions["site"] = "hers"
        self.assertIsNone(await self.app.on_claude_code_permission(self.request("hers")))
        self.assertIsNone(await self.app.on_claude_code_permission(self.request(tool="AskUserQuestion")))
        self.assertEqual(self.on_signal, [])

    async def test_coming_back_or_answering_in_claude_code_withdraws_it(self):
        self.answer = None
        asking = asyncio.create_task(self.app.on_claude_code_permission(self.request()))
        while not self.on_signal:
            await asyncio.sleep(0.01)
        self.app.standby = False
        self.assertIsNone(await asyncio.wait_for(asking, 1))
        self.app.standby = True
        asking = asyncio.create_task(self.app.on_claude_code_permission(self.request("other")))
        while len(self.on_signal) < 2:
            await asyncio.sleep(0.01)
        self.app.claude_sessions.update({"hook_event_name": "PostToolUse", "session_id": "other"})
        self.assertIsNone(await asyncio.wait_for(asking, 1))


class RunToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "Development" / "cora").mkdir(parents=True)
        self.started = []
        runner = SimpleNamespace(current=None, busy=False, sessions={},
                                 start=lambda *args: self.started.append(args), stop=AsyncMock(return_value=True))
        self.confirmations = SimpleNamespace(ask=AsyncMock(return_value=APPROVED))
        self.app = SimpleNamespace(
            claude_code_listen=True, claude_code_run=True, claude_code_folders=[str(self.root / "Development")],
            claude_code_runner=runner, claude_sessions=SessionTracker(), confirmations=self.confirmations,
            activity=MagicMock(),
        )
        self.app.can_run_claude_code = lambda: ClaudeCodeMixin.can_run_claude_code(self.app)
        patcher = patch("app_areas.claude_code.subscription_problem", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_it(self, action="start", project="site", create=True, location=None, brief="make a site"):
        return await ClaudeCodeMixin.run_claude_code(self.app, action, project, create, location, brief)

    def test_its_arguments(self):
        good = {"action": "start", "project": "site", "create": True, "location": None, "brief": "make it"}
        validate_arguments(good)
        validate_arguments({**good, "action": "stop", "project": None, "brief": None, "create": False})
        for bad in ({**good, "action": "delete"}, {**good, "brief": None}, {**good, "project": " "},
                    {**good, "create": "yes"}, {**good, "brief": "x" * 8001}, {k: v for k, v in good.items() if k != "brief"}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    def test_only_offered_when_settings_allow_it(self):
        app = SimpleNamespace(maps_key="k", calling_out_problem=lambda: None, claude_code_listen=True,
                              claude_code_run=False, claude_code_folders=["D:\\"])
        app.can_run_claude_code = lambda: ClaudeCodeMixin.can_run_claude_code(app)
        self.assertIn("run_claude_code", WebInterfaceMixin.unavailable_tools(app))
        app.claude_code_run = True
        self.assertNotIn("run_claude_code", WebInterfaceMixin.unavailable_tools(app))
        app.claude_code_folders = []
        self.assertIn("run_claude_code", WebInterfaceMixin.unavailable_tools(app))

    async def test_a_new_project_starts_once_you_approve_its_brief(self):
        result = await self.run_it()
        self.assertEqual(result["status"], "started")
        folder = self.root / "Development" / "site"
        self.confirmations.ask.assert_awaited_once_with(
            "claude_code", "new project", {"project": "site", "folder": str(folder), "brief": "make a site"},
            title="Claude Code")
        self.assertEqual(self.started, [("site", folder, "make a site", None, False)])

    async def test_nothing_starts_without_your_yes(self):
        self.confirmations.ask.return_value = DECLINED
        self.assertEqual((await self.run_it())["status"], "declined")
        self.assertEqual(self.started, [])

    async def test_unknown_projects_and_settings_that_dont_allow_it_say_why(self):
        missing = await self.run_it(project="rocky", create=False)
        self.assertIn("no project called rocky", missing["message"])
        self.app.claude_code_run = False
        self.assertIn("Settings > Claude Code", (await self.run_it())["message"])
        self.confirmations.ask.assert_not_awaited()

    async def test_continuing_picks_her_last_session_or_forks_yours(self):
        folder = self.root / "Development" / "cora"
        self.assertIn("no earlier session", (await self.run_it("continue", "cora", False))["message"])
        self.app.claude_sessions.update({"hook_event_name": "Stop", "session_id": "vscode", "cwd": str(folder)})
        await self.run_it("continue", "cora", False, brief="add tests")
        self.assertEqual(self.started[-1], ("cora", folder, "add tests", "vscode", True))
        self.app.claude_code_runner.sessions[str(folder).casefold()] = "hers"
        await self.run_it("continue", "cora", False, brief="more")
        self.assertEqual(self.started[-1], ("cora", folder, "more", "hers", False))

    async def test_one_at_a_time_and_stopping(self):
        self.app.claude_code_runner.busy = True
        self.app.claude_code_runner.current = SimpleNamespace(project="cora")
        self.assertIn("already working on cora", (await self.run_it())["message"])
        self.assertIn("Stopping", (await self.run_it("stop", None, False, brief=None))["message"])

    async def test_the_tool_sends_the_result_back_and_she_carries_on(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        run = AsyncMock(return_value={"status": "started"})
        tool = RunClaudeCodeTool(lambda: connection, lambda: False, run, MagicMock())
        await tool.handle("call-1", {"action": "start", "project": " site ", "create": True, "location": " ",
                                     "brief": "make it"})
        run.assert_awaited_once_with("start", "site", True, None, "make it")
        sent = connection.response.item.create.await_args.kwargs["item"]
        self.assertEqual(json.loads(sent["output"])["status"], "started")
        connection.response.create.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
