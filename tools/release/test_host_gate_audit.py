"""Public regression checks for the frozen host evidence audit."""
import json
from pathlib import Path
import tempfile
import unittest

from seal_qualification import audit_host_gate, sha

ROOT = Path(__file__).resolve().parents[2]


class HostGateAuditTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture(self, count):
        names = [f'contract_{i}' for i in range(count)]
        manifest = self.directory / 'required_host_tests.txt'
        manifest.write_text('\n'.join(names) + '\n')
        (self.directory / 'result.json').write_text(json.dumps({
            'passed': True, 'exit': 0, 'required_tests': names,
            'manifest_sha256': sha(manifest)}))
        (self.directory / 'run.log').write_text(
            ''.join(f'[PASS] {name}\n' for name in names) +
            f'{count} tests, {count} passed, 0 failed, 0 skipped\n')

    def test_historical_and_extended_manifest(self):
        for count in [22, 30]:
            with self.subTest(count=count):
                self.fixture(count)
                self.assertEqual(audit_host_gate(self.directory), count)

    def test_empty_manifest(self):
        self.fixture(0)
        with self.assertRaises(AssertionError):
            audit_host_gate(self.directory)

    def test_missing_or_skipped_pass(self):
        for replacement in ['', '[SKIP] contract_1\n', '[PASS] contract_0\n']:
            with self.subTest(replacement=replacement):
                self.fixture(2)
                log = self.directory / 'run.log'
                log.write_text(log.read_text().replace(
                    '[PASS] contract_1\n', replacement))
                with self.assertRaises(AssertionError):
                    audit_host_gate(self.directory)

    def test_manifest_drift(self):
        self.fixture(2)
        (self.directory / 'required_host_tests.txt').write_text('other\n')
        with self.assertRaises(AssertionError):
            audit_host_gate(self.directory)


if __name__ == '__main__':
    unittest.main()
