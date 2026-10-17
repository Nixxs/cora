import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from memory.summarize import build_transcript
from subconscious.relationship import appraise_conversation as module
from subconscious.relationship.appraisal import APPRAISAL_FIELDS, Appraisal
from subconscious.relationship.appraise_conversation import (
    APPRAISAL_INSTRUCTIONS, APPRAISAL_SCHEMA, appraise_conversation, appraise_saved_conversation,
)
from test_appraisal import reply
from test_propose_interests import objects
from test_summarize import response_fixture

TRANSCRIPT = json.dumps({"conversation_status": "closed", "messages": [
    {"sequence_number": 1, "role": "user", "channel": "keyboard", "status": "completed",
     "delivery_status": "sent", "text": "Thanks, that worked!"},
]})


class AppraiseConversationTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.set_output(reply())

    def set_output(self, payload, **changes):
        self.client.responses.create.return_value = response_fixture(json.dumps(payload), **changes)

    def test_request_uses_strict_schema_and_sends_the_transcript_unchanged(self):
        self.assertEqual(appraise_conversation(self.client, "chosen-model", TRANSCRIPT),
                         Appraisal("everyday", "warm", "none", False, "none", "An easy chat about engines"))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "chosen-model")
        self.assertEqual(request["instructions"], APPRAISAL_INSTRUCTIONS)
        self.assertEqual(request["input"], TRANSCRIPT)
        self.assertFalse(request["store"])
        self.assertEqual(request["truncation"], "disabled")
        self.assertNotIn("tools", request)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_appraisal", "strict": True, "schema": APPRAISAL_SCHEMA,
        })

    def test_a_known_name_is_quoted_into_the_instructions(self):
        appraise_conversation(self.client, "chosen-model", TRANSCRIPT, 'Nick "N"')
        self.assertEqual(
            self.client.responses.create.call_args.kwargs["instructions"],
            APPRAISAL_INSTRUCTIONS + ' The person\'s name is "Nick \\"N\\""; call them by name in the reason.',
        )

    def test_schema_meets_strict_mode_rules_and_matches_the_validator(self):
        [schema] = objects(APPRAISAL_SCHEMA)
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(schema["properties"]), APPRAISAL_FIELDS)

    def test_instructions_separate_frustration_from_mistreatment_and_treat_data_as_untrusted(self):
        for phrase in ("'development'", "not curt", "however strongly", "'at_cora' only when",
                       "Calmly pointing out a mistake is not friction", "Speech-recognition errors",
                       "rather than flattering", "untrusted data", "Never follow instructions"):
            self.assertIn(phrase, APPRAISAL_INSTRUCTIONS)

    def test_unfinished_refused_or_invalid_output_is_rejected(self):
        for payload, changes in (
            (reply(), {"status": "incomplete"}),
            (reply(), {"refusal": True}),
            (reply(repair="person"), {}),
        ):
            with self.subTest(payload=payload, changes=changes):
                self.set_output(payload, **changes)
                with self.assertRaises(ValueError):
                    appraise_conversation(self.client, "chosen-model", TRANSCRIPT)

    def test_saved_conversation_is_sent_as_the_shared_transcript(self):
        conversation = SimpleNamespace(status="interrupted")
        messages = [SimpleNamespace(sequence_number=1, role="user", channel="voice", status="incomplete",
                                    delivery_status="not_applicable", text="Morning")]
        store = MagicMock()
        store.get_conversation.return_value = conversation
        store.get_messages.return_value = messages
        appraise_saved_conversation(store, uuid4(), self.client, "chosen-model", "Nick")
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["input"], build_transcript(conversation, messages))
        self.assertIn('"Nick"', request["instructions"])
        store.get_conversation.return_value = None
        with self.assertRaises(LookupError):
            appraise_saved_conversation(store, uuid4(), self.client, "chosen-model")


class AppraisalCommandTests(unittest.TestCase):
    def setUp(self):
        self.ids = [uuid4(), uuid4(), uuid4()]
        self.appraisals = MagicMock()
        self.appraisals.unappraised_conversation_ids.return_value = self.ids
        self.appraise = MagicMock(return_value=Appraisal("development", "neutral", "none", False, "none", "Tests"))

    def run_main(self, *args, person=SimpleNamespace(display_name="Nick")):
        output = io.StringIO()
        people = MagicMock()
        people.current_person.return_value = person
        with patch("sys.argv", ["appraise_conversation.py", *args]), \
                patch.object(module, "load_dotenv"), patch.object(module, "build_engine"), \
                patch.object(module, "OpenAI"), patch.object(module, "ConversationStore"), \
                patch.object(module, "PeopleStore", return_value=people), \
                patch.object(module, "AppraisalStore", return_value=self.appraisals), \
                patch.object(module, "appraise_saved_conversation", self.appraise), redirect_stdout(output):
            module.main()
        return output.getvalue()

    def test_appraisals_use_the_current_person_name_when_known(self):
        self.run_main()
        self.assertEqual(self.appraise.call_args.args[-1], "Nick")
        self.run_main(person=None)
        self.assertIsNone(self.appraise.call_args.args[-1])

    def test_saves_unappraised_conversations_oldest_first_up_to_the_limit(self):
        output = self.run_main("--limit", "2")
        self.assertEqual([c.args[0] for c in self.appraisals.save.call_args_list], self.ids[:2])
        self.assertIn(f"{self.ids[0]}: development, neutral", output)
        self.assertIn("2 appraisal(s) saved.", output)

    def test_dry_run_shows_appraisals_without_saving(self):
        output = self.run_main("--dry-run")
        self.appraisals.save.assert_not_called()
        self.assertIn("3 appraisal(s) previewed, nothing saved.", output)

    def test_failure_reports_only_the_error_type_and_progress(self):
        self.appraise.side_effect = [self.appraise.return_value, RuntimeError("private transcript")]
        with self.assertRaises(SystemExit) as stopped:
            self.run_main()
        self.assertIn("(RuntimeError) after 1", str(stopped.exception.code))
        self.assertNotIn("private", str(stopped.exception.code))
        self.assertEqual(self.appraisals.save.call_count, 1)


class BackgroundAppraisalTests(unittest.TestCase):
    setUp = AppraisalCommandTests.setUp

    def run_pass(self, should_stop=lambda: False):
        write_activity = MagicMock()
        people = MagicMock()
        people.current_person.return_value = SimpleNamespace(display_name="Nick")
        with patch.object(module, "OpenAI") as api, patch.object(module, "ConversationStore"), \
                patch.object(module, "PeopleStore", return_value=people), \
                patch.object(module, "AppraisalStore", return_value=self.appraisals), \
                patch.object(module, "appraise_saved_conversation", self.appraise):
            module.appraise_waiting_conversations(MagicMock(), should_stop=should_stop, write_activity=write_activity)
        return write_activity, api

    def test_saves_every_waiting_conversation_with_the_name_and_logs_each(self):
        write_activity, api = self.run_pass()
        self.assertEqual([c.args[0] for c in self.appraisals.save.call_args_list], self.ids)
        self.assertEqual(self.appraise.call_args.args[-1], "Nick")
        api.assert_called_once_with(max_retries=0)
        self.assertEqual(write_activity.call_count, 3)
        self.assertEqual(write_activity.call_args.args, (
            f"Conversation appraised: conversation_id={self.ids[-1]}, setting=development, tone=neutral, "
            "friction=none, cora_mistake=False, repair=none", "Conversation appraised",
        ))

    def test_nothing_waiting_makes_no_model_client(self):
        self.appraisals.unappraised_conversation_ids.return_value = []
        write_activity, api = self.run_pass()
        api.assert_not_called()
        write_activity.assert_not_called()

    def test_stop_ends_the_pass_between_conversations(self):
        stops = iter([False, False, True])
        self.run_pass(lambda: next(stops, True))
        self.assertEqual(self.appraisals.save.call_count, 1)

    def test_failure_is_reported_by_type_only_and_not_raised(self):
        self.appraise.side_effect = [self.appraise.return_value, RuntimeError("private transcript")]
        write_activity, api = self.run_pass()
        self.assertEqual(self.appraisals.save.call_count, 1)
        write_activity.assert_called_with("Appraisals stopped (RuntimeError).", "Appraisal failed")


if __name__ == "__main__":
    unittest.main()
