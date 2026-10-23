"""Synthetic log contracts; no HTTP, subprocess, model or GPU is started."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from run_stage import EnvironmentAbort, ROOT, audit_server, require_monitor


class SuffixAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix='suffix-audit-', dir=ROOT / '.q4t-work')
        self.output = Path(self.temporary.name)
        self.prefix = ('[request] event=admitted id=first\n'
                       '[request] event=admitted id=second\n')
        self.final = ('[startup-reservation] '
                      '{"event":"finalized","status":"PASS",'
                      '"release_unknown":0}\n')
        self.suffix = ('[suffix] request=first queries=3 hits=1 invalid=0 exhausted=0\n'
                       '[suffix] request=second queries=4 hits=2 invalid=0 exhausted=0\n')
        (self.output / 'exit.json').write_text(json.dumps(
            {'server': 0, 'http_output_checks_passed': True}))

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, suffix=None, enabled=True):
        (self.output / 'server.log').write_text(
            self.prefix + (self.suffix if suffix is None else suffix) + self.final)
        (self.output / 'server-command.json').write_text(json.dumps(
            {'argv': ['q4t', 'serve'] + (['--mtp-suffix'] if enabled else [])}))

    def test_enabled_requires_and_records_every_summary(self):
        self.write()
        audit_server(self.output, 2)
        record = json.loads((self.output / 'server-audit.json').read_text())
        self.assertEqual(len(record['suffix']), 2)
        self.assertEqual(len(record['mtp_records']), 2)
        self.assertEqual(record['suffix'][1]['hits'], '2')

    def test_missing_summary_is_not_silent_success(self):
        self.write(self.suffix.splitlines()[0] + '\n')
        with self.assertRaisesRegex(RuntimeError, 'per-request'):
            audit_server(self.output, 2)

    def test_disabled_invalid_and_exhausted_are_failures(self):
        cases = [self.suffix + '[suffix] request=first disabled=allocation failed\n',
                 self.suffix.replace('invalid=0', 'invalid=1', 1),
                 self.suffix.replace('exhausted=0', 'exhausted=1', 1)]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.write(value)
                audit_server(self.output, 2)

    def test_off_never_silently_uses_suffix(self):
        self.write(enabled=False)
        with self.assertRaisesRegex(RuntimeError, 'suffix-off'):
            audit_server(self.output, 2)
        self.write('', enabled=False)
        audit_server(self.output, 2)

    def test_live_but_stalled_sampler_is_environment_abort(self):
        sampler = SimpleNamespace(is_alive=lambda: True)
        with patch('run_stage.time.monotonic', return_value=16):
            with self.assertRaises(EnvironmentAbort):
                require_monitor(sampler, [], {'last_sample': 10}, 5)
            require_monitor(sampler, [], {'last_sample': 12}, 5)

    def test_dead_or_failed_sampler_is_environment_abort(self):
        for alive, failures in [(False, []), (True, ['read failed'])]:
            with self.subTest(alive=alive, failures=failures):
                sampler = SimpleNamespace(is_alive=lambda: alive)
                with patch('run_stage.time.monotonic', return_value=10):
                    with self.assertRaises(EnvironmentAbort):
                        require_monitor(sampler, failures, {'last_sample': 10}, 5)


if __name__ == '__main__':
    unittest.main()
