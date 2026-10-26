"""Which conversation a tool's result goes back to: Live by default, or a chat without Live.

Tools reply through `connection.response.item.create(...)` and `connection.response.create()`.
A chat without Live hands them a route with those same two methods, so every tool works in
both without knowing which it is in.
"""

from contextvars import ContextVar

current_route: ContextVar = ContextVar("tool_route", default=None)


def routed(get_live_connection):
    """The connection tools reply to: the route of the call being handled, else Live's."""
    def get_connection():
        route = current_route.get()
        return route if route is not None else get_live_connection()
    return get_connection
