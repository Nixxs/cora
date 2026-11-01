"""Freeze Cora's monotonic clock in app.py and every app_areas module that reads it.

    with frozen_monotonic(1000) as clock: ...      # or, on a test method:
    @frozen_monotonic(1000)
    async def test_something(self, clock): ...

The same mock is patched everywhere, so setting clock.return_value moves time for all of them.
"""

from contextlib import ExitStack
import functools
import importlib
import inspect
import pkgutil
from unittest.mock import MagicMock, patch

import app_areas


def clock_modules():
    names = ["app"] + [f"app_areas.{info.name}" for info in pkgutil.iter_modules(app_areas.__path__)]
    modules = (importlib.import_module(name) for name in names)
    return [module for module in modules if hasattr(module, "monotonic")]


class frozen_monotonic:
    def __init__(self, return_value=0.0):
        self.return_value = return_value
        self.stack = None

    def __enter__(self):
        clock = MagicMock(return_value=self.return_value)
        self.stack = ExitStack()
        for module in clock_modules():
            self.stack.enter_context(patch.object(module, "monotonic", clock))
        return clock

    def __exit__(self, *exc_info):
        return self.stack.__exit__(*exc_info)

    def __call__(self, function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapper(*args, **kwargs):
                with frozen_monotonic(self.return_value) as clock:
                    return await function(*args, clock, **kwargs)
        else:
            @functools.wraps(function)
            def wrapper(*args, **kwargs):
                with frozen_monotonic(self.return_value) as clock:
                    return function(*args, clock, **kwargs)
        return wrapper
