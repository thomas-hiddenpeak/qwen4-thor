"""CPU rollback contracts using temporary files only."""
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stage


class PairContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.build, self.old, self.new = [root / n for n in ("build", "old", "new")]
        for directory in (self.build, self.old, self.new):
            directory.mkdir()
        for name in stage.NAMES:
            (self.old / name).write_bytes(("old-" + name).encode())
            (self.new / name).write_bytes(("new-" + name).encode())
        self.oh = {n: stage.h.sha(self.old / n) for n in stage.NAMES}
        self.nh = {n: stage.h.sha(self.new / n) for n in stage.NAMES}

    def installed(self, bits=(True, True)):
        for name, bit in zip(stage.NAMES, bits):
            (self.build / name).write_bytes(((self.new if bit else self.old) / name).read_bytes())

    def restore(self):
        stage.restore_pair(self.build, self.old, self.new, self.oh, self.nh)

    def test_all_complete_and_partial_pairs(self):
        for bits in itertools.product((False, True), repeat=2):
            self.installed(bits)
            self.restore()
            stage.verify_hashes(self.build, self.oh)

    def test_unknown_second_file_blocks_first_write(self):
        self.installed()
        (self.build / "q4t.release.json").write_bytes(b"foreign")
        with self.assertRaises(RuntimeError):
            self.restore()
        self.assertEqual(stage.h.sha(self.build / "q4t"), self.nh["q4t"])

    def test_corrupt_previous_blocks_restoration(self):
        self.installed()
        (self.old / "q4t.release.json").write_bytes(b"corrupt")
        with self.assertRaises(RuntimeError):
            self.restore()
        stage.verify_hashes(self.build, self.nh)

    def test_corrupt_candidate_blocks_restoration(self):
        self.installed()
        (self.new / "q4t").write_bytes(b"corrupt")
        with self.assertRaises(RuntimeError):
            self.restore()
        stage.verify_hashes(self.build, self.nh)

    def test_failed_second_replace_recovers_known_partial_pair(self):
        self.installed()
        original = stage.h.replace_checked

        def fail_second(source, destination, current):
            if Path(destination).name == "q4t.release.json":
                raise OSError("injected replace failure")
            original(source, destination, current)

        with patch.object(stage.h, "replace_checked", fail_second):
            with self.assertRaises(OSError):
                self.restore()
        self.assertEqual(stage.h.sha(self.build / "q4t"), self.oh["q4t"])
        self.restore()
        stage.verify_hashes(self.build, self.oh)

    def test_symlink_destination_rejected(self):
        self.installed()
        (self.build / "q4t").unlink()
        (self.build / "q4t").symlink_to(self.new / "q4t")
        with self.assertRaises(RuntimeError):
            self.restore()


if __name__ == "__main__":
    unittest.main()
