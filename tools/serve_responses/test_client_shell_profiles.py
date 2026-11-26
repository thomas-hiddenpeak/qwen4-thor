import copy
import unittest

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import client_shell_profiles as shell


class ShellHandoff(unittest.TestCase):
    def test_capability_is_one_exact_step_from_the_patch_profile(self):
        previous = {"output_items": ["message", "apply_patch_call", "compaction"], "store": True}
        current = reuse.client_shell_capability(previous)
        self.assertEqual(current["output_items"], ["message", "apply_patch_call", "shell_call", "compaction"])
        self.assertEqual(current["shell"], {"executor": "client", "environments": ["local"],
                         "automatic_execution": False, "result_item": "shell_call_output"})
        self.assertEqual(previous["output_items"], ["message", "apply_patch_call", "compaction"])
        with self.assertRaises(ValueError):
            reuse.client_shell_capability(current)

    def test_original_http_bytes_and_new_result_both_required(self):
        direct = shell.package.read(shell.PROOF)["client"]
        program = direct["program"]
        hashes = {scope + "_" + key: direct["captures"][filename]
                  for scope, names in (("direct", ("call.json", "completed.json", "inputs.json")),
                    ("program", ("program-pending.json", "program-stored.json", "program-inputs.json")))
                  for key, filename in zip(("call", "completed", "inputs"), names, strict=True)}
        hashes.update({k: "a" * 64 for k in ("current_response", "current_inputs", "independent", "create")})
        saved = {"id": "resp_new", "response_sha256": "a" * 64, "inputs_sha256": "a" * 64}
        primary = dict(wire_sha256=hashes, saved=saved, restored=None, program=program["response"],
                       program_follow=program["follow"], original=direct["response"], follow=direct["follow"],
                       result="exit=0 count=2", peer=404, sdk=direct["sdk"], independent_chat="720041")
        shell.check(primary)
        light = copy.deepcopy(primary)
        light.update(saved=dict(saved, id="resp_second"), restored=saved)
        light["wire_sha256"].update(restored_response="a"*64, restored_inputs="a"*64)
        shell.check(light, primary)
        for field in ("program_inputs", "direct_completed", "restored_inputs"):
            broken = copy.deepcopy(light)
            broken["wire_sha256"][field] = "b" * 64
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                shell.check(broken, primary)
        with self.assertRaises(RuntimeError):
            shell.check(dict(primary, result=program["answer"]))
        with self.assertRaises(RuntimeError):
            shell.check(dict(light, saved=primary["saved"]), primary)
