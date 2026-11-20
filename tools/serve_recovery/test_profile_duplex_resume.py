"""Regression for original audio receipt size and explicit light-only start."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from tools.serve_recovery import profile_duplex as io
from tools.serve_recovery import profile_acceptance as full
from tools.serve_service import package

class Receipt(unittest.TestCase):
    def test_audio_receipt_above_metadata_limit_keeps_bytes_without_widening_metadata(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            path=Path(directory)/'observed.json';value=dict(audio='A'*(2<<20))
            io.save_observed(path,value)
            with self.assertRaisesRegex(RuntimeError,'metadata size limit'):package.read(path)
            self.assertEqual(io.read_observed(path),value)
            original=path.read_bytes()
            with self.assertRaises(FileExistsError):io.save_observed(path,value)
            self.assertEqual(path.read_bytes(),original)
            path.write_bytes(b' '*(4<<20)+b'{}')
            with self.assertRaisesRegex(RuntimeError,'receipt size limit'):io.read_observed(path)

    def test_explicit_invalid_profile_cannot_start_a_model(self):
        stage=object.__new__(full.Stage);stage.plan={};stage.counts={'starts':0}
        with self.assertRaisesRegex(RuntimeError,'unknown qualified profile'):stage.start(selected='not_qualified')
        self.assertEqual(stage.counts,{'starts':0})

if __name__=='__main__':unittest.main()
