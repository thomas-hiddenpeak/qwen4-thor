"""Host-only driver lifecycle tests using a fake executable, never CUDA/model."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
DRIVER = Path(__file__).with_name('run.py')
FAKE = '''#!/usr/bin/env python3
import os,sys
from pathlib import Path
mode,model,fixture,output=sys.argv[1:5]
out=Path(output);out.mkdir()
(out/'used-fixture').write_bytes(Path(fixture).read_bytes())
if os.environ.get('REPLAY_FAIL'): sys.exit(7)
for suffix in ['logits.bf16','ids.i32','weights.f32','output.bf16']:
 (out/(('t4-row0-normal.' if mode=='suite' else 'replay.')+suffix)).write_bytes(b'fixed')
if mode=='suite':
 (out/'t4-row0.route').write_bytes(b'route')
 target=os.environ.get('REPLAY_MUTATE')
 if target: Path(fixture if target=='snapshot' else target).write_text('changed')
'''


class DriverTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/moe-replay-driver-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.binary = self.base / 'fake'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.fixture = self.base / 'input.json'
        self.fixture.write_text('{"fixed": true}\n')
        self.output = self.base / 'result'

    def command(self):
        return [sys.executable, str(DRIVER), '--binary', str(self.binary),
                '--fixture', str(self.fixture), '--model', str(self.base),
                '--output', str(self.output)]

    def invoke(self, mutation=None):
        env = dict(os.environ)
        env.pop('REPLAY_MUTATE', None)
        if mutation:
            env['REPLAY_MUTATE'] = mutation
        run = subprocess.run(self.command(), env=env, capture_output=True, text=True)
        return run, json.loads((self.output / 'identity.json').read_text())

    def test_snapshot_used_for_both_processes(self):
        run, record = self.invoke()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(record['status'], 'PASS')
        for command in record['commands']:
            self.assertEqual(Path(command[3]), self.output / 'fixture.json')
        self.assertEqual((self.output / 'suite/used-fixture').read_bytes(),
                         (self.output / 'replay/used-fixture').read_bytes())

    def test_invariance_uses_one_process(self):
        run = subprocess.run(self.command() + ['--suite', 'invariance'],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        record = json.loads((self.output / 'identity.json').read_text())
        self.assertEqual(record['status'], 'PASS')
        self.assertEqual(len(record['commands']), 1)
        self.assertEqual(record['commands'][0][1], 'invariance')
        self.assertFalse((self.output / 'replay').exists())

    def test_invariance_failure_is_not_retried(self):
        run = subprocess.run(self.command() + ['--suite', 'invariance'],
                             env={**os.environ, 'REPLAY_FAIL': '1'},
                             capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        record = json.loads((self.output / 'identity.json').read_text())
        self.assertEqual(record['status'], 'FAIL')
        self.assertEqual(record['invariance_returncode'], 7)
        self.assertEqual(len(record['commands']), 1)

    def test_original_mutation_cannot_change_replay_input(self):
        run, record = self.invoke(str(self.fixture))
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(record['status'], 'FAIL')
        self.assertIn('original fixture changed', record['error'])
        self.assertEqual((self.output / 'suite/used-fixture').read_bytes(),
                         (self.output / 'replay/used-fixture').read_bytes())

    def test_snapshot_corruption_stops_before_second_process(self):
        run, record = self.invoke('snapshot')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(record['status'], 'FAIL')
        self.assertIn('fixture snapshot changed', record['error'])
        self.assertEqual(len(record['commands']), 1)

    def test_missing_input_retains_preparation_failure(self):
        self.binary.unlink()
        run, record = self.invoke()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(record['status'], 'FAIL')
        self.assertIn('FileNotFoundError', record['error'])
        self.assertTrue((self.output / 'files.json').is_file())

    def test_interruption_is_terminal_and_recorded(self):
        spec = importlib.util.spec_from_file_location('replay_driver', DRIVER)
        module = importlib.util.module_from_spec(spec)
        # Avoid writing __pycache__ outside build/ or .q4t-work/.
        exec(compile(DRIVER.read_text(), str(DRIVER), 'exec'), module.__dict__)
        with mock.patch.object(sys, 'argv', self.command()[1:]):
            with mock.patch.object(module.subprocess, 'run',
                                   side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    module.main()
        record = json.loads((self.output / 'identity.json').read_text())
        self.assertEqual(record['status'], 'INTERRUPTED')
        self.assertTrue((self.output / 'files.json').is_file())


if __name__ == '__main__':
    unittest.main()
