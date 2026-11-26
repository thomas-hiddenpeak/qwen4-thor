import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import client_patch_profiles as patches


class ClientPatchHandoff(unittest.TestCase):
    def test_fresh_store_preserves_program_key_and_rejects_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def fixture(label, members):
                path = root / label
                path.mkdir()
                (path / ".q4t-lock").touch()
                rows = dict(members, **{".q4t-format": b"format"})
                for name, data in rows.items():
                    (path / name).write_bytes(data)
                return path, {"records": {k: hashlib.sha256(v).hexdigest() for k, v in rows.items()},
                              "source_bytes": sum(map(len, rows.values()))}
            direct = fixture("direct", {"call": b"direct"})
            program = fixture("program", {"program-key": b"original", "child": b"child"})
            store = root / "joined"
            patches.seed_histories(store, [direct, program])
            self.assertEqual((store / "program-key").read_bytes(), b"original")
            self.assertEqual((store / "call").read_bytes(), b"direct")
            duplicate = fixture("collision", {"call": b"another"})
            with self.assertRaises(RuntimeError):
                patches.seed_histories(root / "failed", [direct, duplicate])
            self.assertFalse((root / "failed").exists())

    def test_exact_upgrade_for_client_patch(self):
        before = {"output_items": ["message", "local_shell_call", "compaction"], "store": True}
        after = reuse.client_patch_capability(before)
        self.assertEqual(after["output_items"], ["message", "local_shell_call", "apply_patch_call", "compaction"])
        self.assertEqual(before["output_items"], ["message", "local_shell_call", "compaction"])
        self.assertEqual(after["apply_patch"]["operations"], ["create_file", "update_file", "delete_file"])
        self.assertFalse(after["apply_patch"]["automatic_execution"])
        with self.assertRaises(ValueError):
            reuse.client_patch_capability(after)

    def test_changed_historical_http_bytes_or_continuation_are_rejected(self):
        direct, program = (patches.package.read(p)["client"] for p in (patches.PROOF, patches.PROGRAM))
        hashes = {f"{scope}_{label}": v["captures"][name]
                  for scope, v, files in (("direct", direct, ("call.json", "completed.json", "inputs.json")),
                                          ("program", program, ("pending.json", "stored.json", "inputs.json")))
                  for label, name in zip(("call", "completed", "inputs"), files, strict=True)}
        hashes.update({k: "a"*64 for k in ("current_response", "current_inputs", "independent", "create")})
        saved = {"id": "resp_new", "response_sha256": "a"*64, "inputs_sha256": "a"*64}
        row = dict(wire_sha256=hashes, saved=saved, restored=None, program=program["response"],
                   program_follow=program["follow"], original=direct["response"], follow=direct["follow"],
                   peer=404, sdk=direct["sdk"], independent_chat="720041", result="failed ENOENT")
        patches.check(row)
        second = copy.deepcopy(row)
        second.update(saved=dict(saved, id="resp_next"), restored=saved)
        second["wire_sha256"].update(restored_response="a"*64, restored_inputs="a"*64)
        patches.check(second, row)
        for field in ("program_inputs", "direct_call", "restored_response"):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(second)
                changed["wire_sha256"][field] = "b"*64
                patches.check(changed, row)
        second["saved"] = saved
        with self.assertRaises(RuntimeError):
            patches.check(second, row)
