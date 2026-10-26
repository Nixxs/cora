"""Tool calls that change things wait here for the person's own answer, one pop-up at a time.

Only something the person does in the app answers a request; nothing the model says or a
service returns can approve one.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count

from .catalogue import SERVICES

APPROVED, DECLINED, TIMED_OUT = "approved", "declined", "timed out"
# How long a request waits for an answer before it counts as declined.
CONFIRM_SECONDS = 120
# When Cora gets one note to remind the person, if the pop-up is still waiting.
REMIND_SECONDS = 45

_ids = count(1)


@dataclass
class ConfirmationRequest:
    service_id: str
    tool: str
    arguments: dict
    # What the pop-up and Cora's notes call it: a service's title, or a name for one of Cora's own tools.
    given_title: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    answer: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    @property
    def title(self) -> str:
        return self.given_title or SERVICES[self.service_id].title


class Confirmations:
    """Queues requests and shows them one at a time; the app supplies how to show and close one."""

    def __init__(self, show: Callable, close: Callable, write_activity: Callable,
                 timeout: float = CONFIRM_SECONDS, remind: Callable = lambda request: None,
                 remind_after: float = REMIND_SECONDS) -> None:
        self.show = show
        self.close = close
        self.write_activity = write_activity
        self.timeout = timeout
        # Called once when a showing request is still unanswered after remind_after seconds.
        self.remind = remind
        self.remind_after = remind_after
        self.waiting: list[ConfirmationRequest] = []

    async def ask(self, service_id: str, tool: str, arguments: dict, title: str | None = None) -> str:
        """Wait for the person's answer: APPROVED, DECLINED or TIMED_OUT.

        `title` names one of Cora's own tools' areas (such as Signal); a service's comes from the catalogue.
        """
        request = ConfirmationRequest(service_id, tool, dict(arguments), title)
        self.waiting.append(request)
        self.write_activity(
            f"{request.title} tool {tool} is waiting for your approval.", f"{request.title} · {tool} needs approval",
        )
        if len(self.waiting) == 1:
            self.show(request)
        try:
            first_wait = min(self.remind_after, self.timeout)
            try:
                approved = await asyncio.wait_for(asyncio.shield(request.answer), first_wait)
            except TimeoutError:
                if first_wait >= self.timeout:
                    raise
                if self.waiting and self.waiting[0] is request:
                    self.remind(request)
                approved = await asyncio.wait_for(asyncio.shield(request.answer), self.timeout - first_wait)
            outcome = APPROVED if approved else DECLINED
        except TimeoutError:
            outcome = TIMED_OUT
        finally:
            showing = self.waiting and self.waiting[0] is request
            self.waiting.remove(request)
            if showing:
                self.close(request)
                if self.waiting:
                    self.show(self.waiting[0])
        self.write_activity(f"{request.title} tool {tool}: {outcome}.", f"{request.title} · {tool} {outcome}")
        return outcome

    def answer(self, request_id: int, approved: bool) -> None:
        for request in self.waiting:
            if request.id == request_id and not request.answer.done():
                request.answer.set_result(approved)

    def decline_all(self) -> None:
        """When closing: nothing waiting may run later."""
        for request in list(self.waiting):
            if not request.answer.done():
                request.answer.set_result(False)
