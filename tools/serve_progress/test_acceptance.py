"""CPU negative controls for the separate R03 validation-only entry point."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress import acceptance as a
from tools.serve_service import package
from tools.serve_recovery import bundle


class DriverTests(unittest.TestCase):
    def test_validation_identity_is_exact_and_production_rejects_it(self):
        metadata = dict(validation_only=True, acceptance='VALIDATION_ONLY_R03')
        decision = dict(status='PENDING_REAL_BUSINESS_R03')
        expected = dict(metadata=a.fingerprint(metadata), decision=a.fingerprint(decision))
        gate = a.validation_gate(package.accepted_release, expected)
        gate(metadata, decision)
        with self.assertRaisesRegex(RuntimeError, 'accepted release'):
            package.accepted_release(metadata, decision)
        with self.assertRaisesRegex(ValueError, 'frozen identity'):
            gate(dict(metadata, binary_sha256='changed'), decision)
        with self.assertRaisesRegex(ValueError, 'frozen identity'):
            gate(metadata, dict(decision, status='PASS_fabricated'))
        self.assertFalse(any(name.startswith('tools/serve_progress/')
            for name in bundle.SOURCES))

    def test_work_is_reserved_before_dispatch_and_never_refunded(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as directory:
            budget = a.Budget(directory, initialize=True, limits=a.LIMITS)
            for name in ('starts', 'chats', 'probes'):
                for expected in range(1, a.LIMITS[name]+1):
                    self.assertEqual(budget.reserve(name, 'cpu-only'), expected)
                with self.assertRaisesRegex(RuntimeError, 'exhausted'):
                    budget.reserve(name, 'must-not-dispatch')
            with self.assertRaisesRegex(ValueError, 'unknown'):
                budget.reserve('seconds', 'not-work')


if __name__ == '__main__':
    unittest.main()
