from copy import deepcopy
import unittest

from session_config import (
    BACKEND_INSTRUCTIONS, CORA_FOUNDATION, LIVE_INSTRUCTIONS, MEMORY_RECALL_STYLE, build_session_config,
)


class SessionConfigTests(unittest.TestCase):
    def test_both_prompts_open_with_the_fixed_foundation(self):
        self.assertTrue(LIVE_INSTRUCTIONS.startswith("You are Cora. " + CORA_FOUNDATION))
        self.assertLess(BACKEND_INSTRUCTIONS.index(CORA_FOUNDATION), 200)
        config = build_session_config([], "[]", interest_context="[]", person_name="Sam")
        for instructions in (config["instructions"], config["delegation"]["responses"]["instructions"]):
            self.assertEqual(instructions.count(CORA_FOUNDATION), 1)
            self.assertLess(instructions.index(CORA_FOUNDATION), instructions.index('"Sam"'))
            self.assertLess(instructions.index(CORA_FOUNDATION), instructions.index("historical memories"))
            self.assertLess(instructions.index(CORA_FOUNDATION), instructions.index("your own current interests"))

    def test_foundation_keeps_integrity_fixed_and_lets_cora_speak_for_herself(self):
        for phrase in (
            "honest, reliable and honourable", "does not change with your mood", "never lie, mislead",
            "give deliberately poor answers", "break a commitment", "matters for someone's safety",
            "never decide which actions or tools you use", "participant, not an interviewer",
            "do not need to end every reply with a question", "own it",
        ):
            self.assertIn(phrase, CORA_FOUNDATION)
        # The old "thoughts and interests eventually" framing is gone; she has them now.
        self.assertNotIn("eventually", LIVE_INSTRUCTIONS)

    def test_both_models_use_quiet_grounded_memory_recall(self):
        config = build_session_config([])
        for instructions in (config["instructions"], config["delegation"]["responses"]["instructions"]):
            self.assertIn(MEMORY_RECALL_STYLE, instructions)
            self.assertIn("Do not announce a lookup", instructions)
            self.assertIn("Wait for the result", instructions)
            self.assertIn("supported by the available context or returned memory", instructions)
            self.assertIn("rather than inventing a memory", instructions)
            self.assertIn("unless the person asks how memory works", instructions)

    def test_default_settings_and_prompts(self):
        config = build_session_config([])
        self.assertEqual(config["model"], "gpt-live-1")
        self.assertEqual(config["instructions"], LIVE_INSTRUCTIONS)
        self.assertEqual(config["delegation"]["type"], "responses")
        backend = config["delegation"]["responses"]
        self.assertEqual(backend["model"], "gpt-6-luna")
        self.assertEqual(backend["instructions"], BACKEND_INSTRUCTIONS)
        self.assertEqual(backend["tools"], [{"type": "web_search"}])
        self.assertEqual(backend["tool_choice"], "auto")
        self.assertFalse(backend["parallel_tool_calls"])
        self.assertEqual(config["audio"], {
            "format": {"type": "audio/pcm", "rate": 24000},
            "output": {"voice": "marin"},
        })

    def test_custom_schemas_follow_hosted_search(self):
        schemas = [
            {"type": "function", "name": "first", "parameters": {"type": "object"}},
            {"type": "function", "name": "second", "parameters": {"type": "object"}},
        ]
        config = build_session_config(schemas)
        self.assertEqual(config["delegation"]["responses"]["tools"], [
            {"type": "web_search"}, *schemas,
        ])

    def test_payloads_and_input_schemas_do_not_share_mutable_state(self):
        schemas = [{
            "type": "function", "name": "example",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        }]
        original = deepcopy(schemas)
        first = build_session_config(schemas)
        second = build_session_config(schemas)
        first["audio"]["output"]["voice"] = "changed"
        first["delegation"]["responses"]["tools"][0]["type"] = "changed"
        first["delegation"]["responses"]["tools"][1]["parameters"]["properties"]["text"]["type"] = "integer"
        self.assertEqual(schemas, original)
        self.assertEqual(second, build_session_config(original))
        schemas[0]["parameters"]["properties"]["text"]["type"] = "number"
        self.assertEqual(second["delegation"]["responses"]["tools"][1:], original)


if __name__ == "__main__":
    unittest.main()