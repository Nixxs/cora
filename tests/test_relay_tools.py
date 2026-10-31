"""Tools built on pass_on each answer with their own arguments, so a call never waits out its timeout."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend_chat import BackendRoute
from tools.client_boundaries import RaiseConcernTool, StopReplyingTool
from tools.close_task import CloseTaskTool


class RelayToolTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, tool_class, arguments, expected_relay_args, route=None):
        route = route or BackendRoute(memory_scope=("client-id", "Sarah"))
        relay = AsyncMock(return_value=None)
        tool = tool_class(lambda: route, lambda: False, MagicMock(), relay)
        await tool.handle("c1", arguments)
        relay.assert_awaited_once_with(*expected_relay_args)
        self.assertEqual(json.loads(route.outputs["c1"])["status"], "ok")

    async def test_close_task_answers(self):
        task_id = uuid4()
        await self.check(CloseTaskTool, {"task_id": str(task_id), "status": "done", "note": " Booked "},
                         (task_id, "done", "Booked"), BackendRoute())

    async def test_raise_concern_answers(self):
        await self.check(RaiseConcernTool, {"concern": "Threatened me"}, ("client-id", "Sarah", "Threatened me"))

    async def test_stop_replying_answers(self):
        await self.check(StopReplyingTool, {}, ("client-id", "Sarah"))


if __name__ == "__main__":
    unittest.main()
