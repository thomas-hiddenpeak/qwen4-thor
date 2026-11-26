"""MCP approval handoff receipt and closed-store integrity checks."""
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_responses import approval_identifiers_profiles as approvals


def receipt(conversation):
    digest = "1" * 64
    direct = json.loads(approvals.LEGACY.read_bytes())
    hashes = {key: digest for key in ("approved_inputs", "denied_inputs", "approved_response",
                                      "denied_response", "create", "independent",
                                      "current_approved", "current_denied", "current_page")}
    saved = dict(conversation=conversation, page_sha256=digest, approved_sha256=digest,
                 denied_sha256=digest, peer=404)
    return dict(sdk=direct["sdk"], original=direct["original"], saved=saved, restored=None,
                decision_conflict=409, mismatch=400, independent_chat="720041", wire_sha256=hashes)


class Checks(unittest.TestCase):
    def test_restore(self):
        primary, light = receipt("conv_primary"), receipt("conv_light")
        light["restored"] = primary["saved"]
        for key in ("approved", "denied", "page"):
            light["wire_sha256"]["restored_" + key] = primary["saved"][key + "_sha256"]
        approvals.check(primary)
        approvals.check(light, primary)
        for key in ("restored_approved", "restored_denied", "restored_page", "approved_response",
                    "denied_response", "approved_inputs", "denied_inputs"):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                broken = copy.deepcopy(light)
                broken["wire_sha256"][key] = "a" * 64
                approvals.check(broken, primary)

    def test_wrong_authority(self):
        for key in ("target", "decision", "response", "projected_sha256"):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = receipt("conv_primary")
                row["original"]["denied"][key] = row["original"]["approved"][key]
                approvals.check(row)
        with self.assertRaises(RuntimeError):
            row = receipt("conv_primary")
            row["saved"]["peer"] = 404.0
            approvals.check(row)

    def test_only_copy_original_bytes_to_a_fresh_store(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            src = root / "closed"
            src.mkdir()
            lock = src / ".q4t-lock"
            lock.touch()
            record = src / "record"
            record.write_bytes(b"original")
            proof = {"records": {record.name: hashlib.sha256(b"original").hexdigest()},
                     "source_bytes": 8}
            witness = root / "source.json"
            witness.write_text(json.dumps(proof))
            dest = root / "copy"
            with patch.object(approvals, "LEGACY", witness):
                with lock.open("rb") as opened:
                    fcntl.flock(opened, fcntl.LOCK_EX)
                    with self.assertRaises(BlockingIOError):
                        approvals.seed(src, dest)
                self.assertFalse(dest.exists())
                approvals.seed(src, dest)
                self.assertEqual((dest / "record").read_bytes(), b"original")
                self.assertEqual((dest / "record").stat().st_mode & 0o777, 0o600)
                with self.assertRaises(RuntimeError):
                    approvals.seed(src, dest)
                record.write_bytes(b"modified")
                other = root / "other"
                with self.assertRaises(RuntimeError):
                    approvals.seed(src, other)
                self.assertFalse(other.exists())
                record.unlink()
                record.symlink_to(dest / "record")
                with self.assertRaises(RuntimeError):
                    approvals.seed(src, other)


if __name__ == "__main__":
    unittest.main()
