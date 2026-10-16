"""No sysfs accesses: adapter snapshots and privileged writes are mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gpu_clock_io as io


class RestrictedIOTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / '.q4t-work/clock-io-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.marker = Path(self.temp.name) / 'marker'
        self.identity = {'devices': {'gpu-gpc-0': {'available': [1260, 1575]}}}
        self.state = dict(identity=self.identity, original=[1575,1575],
                          current=[1575,1575], pending=dict(
                              field='min_freq',value=1260,after=[1260,1575]))
        self.marker.write_text(json.dumps(self.state))
        self.io = io.GpcIO(self.identity, 1575, self.marker)
        self.snapshot = self.enterContext(patch.object(io, 'snapshot', return_value={
            'identity': self.identity, 'range': [1575,1575]}))
        self.run = self.enterContext(patch.object(io.subprocess, 'run'))

    def test_only_expected_fixed_path_is_written(self):
        self.io.write('min_freq',1260)
        self.run.assert_called_once_with(
            ['sudo','-n','/usr/bin/tee','/sys/class/devfreq/gpu-gpc-0/min_freq'],
            input='1260\n',text=True,check=True,capture_output=True,timeout=5)

    def test_invalid_write_requests_never_invoke_privileged_command(self):
        for field,value in [('governor',1260),('../gpu-nvd-0/min_freq',1260),
                            ('max_freq','1260'),('max_freq',True),
                            ('max_freq',1600),('min_freq',0)]:
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                self.io.write(field,value)
        self.run.assert_not_called()

    def test_identity_or_current_change_is_rejected(self):
        for identity,values in [({'changed':True},[1575,1575]),
                                (self.identity,[1260,1575])]:
            self.snapshot.return_value={'identity':identity,'range':values}
            with self.assertRaises(RuntimeError):self.io.write('min_freq',1260)
        self.run.assert_not_called()

    def test_absent_or_unmatched_intent_is_rejected(self):
        self.marker.unlink()
        with self.assertRaises(FileNotFoundError):self.io.write('min_freq',1260)
        self.state['pending']['field']='max_freq'
        self.marker.write_text(json.dumps(self.state))
        with self.assertRaises(RuntimeError):self.io.write('min_freq',1260)
        self.run.assert_not_called()


if __name__ == '__main__':unittest.main()
