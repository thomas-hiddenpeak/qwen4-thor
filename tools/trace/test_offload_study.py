"""Sort adapter and failure-preserving orchestration contracts."""
import argparse
from array import array
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from run_offload_study import execute, token_order, validate_plan

ROOT = Path(__file__).resolve().parents[2]
HELPER = None


class StudyTest(unittest.TestCase):
    def test_sort_preserves_rows_and_router_order(self):
        rows = [list(range(10, 20)), list(reversed(range(10))),
                list(range(10)), list(range(20, 30))] * 20
        flat = array('H', [v for row in rows for v in row])
        before = flat.tobytes()
        order = token_order(flat, len(rows), HELPER)
        self.assertEqual(sorted(order), list(range(len(rows))))
        keys = [tuple(sorted(rows[i])) for i in order]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(flat.tobytes(), before)

    def test_sort_refuses_bad_shape_and_payload(self):
        with self.assertRaises(ValueError):
            token_order(array('H', range(10)), 2, HELPER)
        for raw in [struct.pack('<II', 0, 10),
                    struct.pack('<II', 1, 10) + b'\0' * 20,
                    struct.pack('<II', 1, 10) + b'\xff' * 20,
                    struct.pack('<II', 8193, 10)]:
            result = subprocess.run([str(HELPER)], input=raw, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_single_token_requires_no_process(self):
        self.assertEqual(token_order(array('H', range(10)), 1, Path('/missing')), [0])

    def test_unfrozen_policies_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(dict(schema=1, policies=['new_tuned_policy'], schedules=[]))

    def test_failure_kept_and_output_never_reused(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as tmp:
            path = Path(tmp)
            out = path / 'run'
            rc = execute(path / 'missing-plan', path / 'missing-layout', out)
            self.assertEqual(rc, 1)
            state = json.loads((out / 'exit.json').read_text())
            self.assertFalse(state['complete'])
            self.assertFalse(state['performance_acceptance'])
            self.assertTrue(state['failure'].startswith('FileNotFoundError'))
            original = (out / 'exit.json').read_bytes()
            with self.assertRaises(FileExistsError):
                execute(path / 'missing-plan', path / 'missing-layout', out)
            self.assertEqual((out / 'exit.json').read_bytes(), original)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sort-helper', type=Path, required=True)
    args, rest = ap.parse_known_args()
    HELPER = args.sort_helper.resolve()
    unittest.main(argv=['test_offload_study.py'] + rest)
