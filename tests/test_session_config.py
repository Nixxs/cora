from copy import deepcopy
import unittest

from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS, build_session_config


class SessionConfigTests(unittest.TestCase):
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