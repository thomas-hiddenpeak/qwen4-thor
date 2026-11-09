"""Direct cache input contracts and untouched C++ response/SSE values."""
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_responses.test_contract import errors, wire_values


class CacheContract(unittest.TestCase):
    def test_modern_ttl_and_block_controls(self):
        for ttl in ("30m", "12h"):
            request = dict(model="cpu", input=[dict(role="user", content=[
                dict(type="input_text", text="prefix",
                     prompt_cache_breakpoint=dict(mode="explicit", ttl=ttl))])],
                prompt_cache_options=dict(mode="explicit", ttl=ttl, prewarm=True),
                prompt_cache_key="键" * 512)
            self.assertEqual(errors(request, "response_create"), [])
            request["prompt_cache_options"]["ttl"] = "24h"
            self.assertTrue(errors(request, "response_create"))
        request["prompt_cache_options"]["ttl"] = "30m"
        request["prompt_cache_key"] += "键"
        self.assertTrue(errors(request, "response_create"))

    def test_chat_controls_and_native_conflicts(self):
        validator = Draft202012Validator(chat_schema())
        request = dict(model="cpu", messages=[dict(role="user", content="hello")],
                       prompt_cache_key="客户", prompt_cache_retention="24h")
        self.assertEqual(list(validator.iter_errors(request)), [])
        request["prefix_cache_scope"] = "a" * 64
        self.assertTrue(list(validator.iter_errors(request)))
        request["prompt_cache_key"] = None
        request["prompt_cache_retention"] = None
        self.assertEqual(list(validator.iter_errors(request)), [])

    def test_actual_cpp_terminal_values(self):
        root = Path(os.environ["Q4T_TEST_OUTPUT_DIR"])
        seen = []
        for path, name, value in wire_values(root):
            if not path.name.startswith("cache-prewarm."):
                continue
            self.assertEqual(errors(value, name), [], str(path))
            seen.append(name)
        self.assertEqual(sorted(seen), ["response", "response_event"])


if __name__ == "__main__":
    unittest.main()
