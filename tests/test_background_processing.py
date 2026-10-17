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

    def test_summaries_done_fires_before_embeddings_and_appraisals_run_last(self):
        order = MagicMock()
        store = order.store
        store.get_unsummarized_conversation_ids.return_value = []
        order.memory_store.get_pending_embedding_ids.return_value = []
        order.memory_store.get_pending_deduplication_ids.return_value = []
        should_stop, write_activity = (lambda: False), MagicMock()
        with patch("memory.background.build_engine") as build, \
                patch("memory.background.ConversationStore", return_value=store), \
                patch("memory.background.MemoryStore", return_value=order.memory_store), \
                patch("memory.background.appraise_waiting_conversations", order.appraise):
            run_background_memory_pass(
                uuid4(), should_stop=should_stop, write_activity=write_activity,
                summaries_done=order.summaries_done,
            )
        names = [call[0] for call in order.mock_calls]
        self.assertLess(names.index("summaries_done"), names.index("memory_store.get_pending_embedding_ids"))
        self.assertEqual(names[-1], "appraise")
        order.appraise.assert_called_once_with(
            build.return_value, should_stop=should_stop, write_activity=write_activity,
        )

    def test_stopped_pass_skips_appraisals(self):
        stops = iter([False, True])
        with patch("memory.background.build_engine"), patch("memory.background.ConversationStore"), \
                patch("memory.background.appraise_waiting_conversations") as appraise:
            run_background_memory_pass(
                uuid4(), should_stop=lambda: next(stops, True), write_activity=MagicMock(),
            )
        appraise.assert_not_called()


if __name__ == "__main__":
    unittest.main()