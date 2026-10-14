"""The background pass can run without a Textual app or worker."""

import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from memory.background import run_background_memory_pass


class BackgroundProcessingTests(unittest.TestCase):
    def test_already_stopped_pass_does_not_open_database_or_log(self):
        write_activity = MagicMock()
        with patch("memory.background.build_engine") as build:
            run_background_memory_pass(
                uuid4(), should_stop=lambda: True, write_activity=write_activity,
            )
        build.assert_not_called()
        write_activity.assert_not_called()

    def test_engine_creation_failure_logs_only_exception_type(self):
        write_activity = MagicMock()
        with patch("memory.background.build_engine", side_effect=RuntimeError("private credentials")):
            run_background_memory_pass(
                uuid4(), should_stop=lambda: False, write_activity=write_activity,
            )
        write_activity.assert_called_once_with(
            "Background memory pass stopped (RuntimeError).", "Memory processing failed",
        )


if __name__ == "__main__":
    unittest.main()