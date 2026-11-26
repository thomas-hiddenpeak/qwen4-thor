"""Detect lost shell response history and unqualified capability changes."""
import copy
import unittest
from unittest.mock import patch

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import local_shell_profiles as shell


class Checks(unittest.TestCase):
    def test_qualified_responses_change_preserves_issued_declarations(self):
        prior = {"output_items": ["message", "custom_tool_call", "compaction"], "store": True}
        upgraded = reuse.local_shell_capability(prior)
        self.assertEqual(upgraded["output_items"], ["message", "custom_tool_call", "local_shell_call", "compaction"])
        self.assertEqual(upgraded["local_shell"], {"executor": "client", "automatic_execution": False,
                                                   "result_item": "local_shell_call_output"})
        self.assertEqual(prior["output_items"], ["message", "custom_tool_call", "compaction"])
        with self.assertRaises(ValueError):
            reuse.local_shell_capability(upgraded)

    def test_preserve_historical_and_current_continuations(self):
        source = dict(sdk="test-sdk", response="resp_call", follow="resp_follow", answer="qwen4_exp",
                      captures={k + ".json": str(i) * 64 for i, k in enumerate(("call", "completed", "inputs"))})
        def receipt(ident):
            names = ("create", "independent", "current_response", "current_inputs")
            hashes = {k: chr(97 + i) * 64 for i, k in enumerate(names)}
            hashes.update({k[:-5]: v for k, v in source["captures"].items()})
            saved = dict(id=ident, response_sha256=hashes["current_response"], inputs_sha256=hashes["current_inputs"])
            return dict(sdk=source["sdk"], original=source["response"], follow=source["follow"], peer=404,
                        answer=source["answer"], independent_chat="720041", saved=saved,
                        restored=None, wire_sha256=hashes)
        first, second = receipt("resp_first"), receipt("resp_second")
        second["restored"] = copy.deepcopy(first["saved"])
        for name in ("response", "inputs"):
            second["wire_sha256"]["restored_" + name] = first["saved"][name + "_sha256"]
        with patch.object(shell.package, "read", return_value={"client": source}):
            shell.check(first)
            shell.check(second, first)
            for name in ("call", "completed", "inputs", "restored_response", "restored_inputs"):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    broken = copy.deepcopy(second)
                    broken["wire_sha256"][name] = "f" * 64
                    shell.check(broken, first)
            second["saved"]["id"] = first["saved"]["id"]
            with self.assertRaises(RuntimeError):
                shell.check(second, first)
