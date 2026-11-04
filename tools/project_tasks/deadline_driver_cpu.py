"""Guard entry checks before any test listener, file receipt, or model start."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import deadline_stage as s
from tools.serve_service import package as pkg


class Entry(unittest.TestCase):
    def test_guard_and_worker_use_absolute_paths(self):
        argv = s.guard_command()
        self.assertEqual(argv[2], str(ROOT/'tools/evalscope/gpu_exclusive.py'))
        self.assertEqual(argv[4], str(s.OUT/'lease'))
        self.assertEqual(argv[8], str(ROOT/'tools/project_tasks/deadline_stage.py'))
        self.assertEqual(argv[9], 'run')

    def test_unowned_entry_stops_before_probe_or_receipt(self):
        run = s.Run({})
        with patch.object(s.prior.h, 'require_guard', side_effect=RuntimeError('unowned lease')) as gate, \
                patch.object(s.pkg, 'save') as save, patch.object(run, 'business') as business:
            with self.assertRaisesRegex(RuntimeError, 'unowned lease'):
                run.execute()
            gate.assert_called_once_with(s.OUT)
            save.assert_not_called()
            business.assert_not_called()


def main():
    os.umask(0o077)
    label = sys.argv[1] if len(sys.argv) == 2 else 'cpu-driver'
    pkg.require(label in ('cpu-driver', 'cpu-driver-final'), 'explicit driver evidence directory')
    out = s.BASE/label
    out.mkdir(mode=0o700)
    names = (s.HERE+'deadline_driver_cpu.py', s.HERE+'deadline_stage.py', s.HERE+'deadline_probes.py',
             s.HERE+'deadline_report.py', s.HERE+'protocol_stage.py')
    sources = {name:pkg.sha(ROOT/name) for name in names}
    for name in names:
        dest = out/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Entry)
    identities = [item.id() for item in suite]
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    (out/'tests.log').write_text(output.getvalue())
    record = dict(passed=result.wasSuccessful(), tests_run=result.testsRun, failures=len(result.failures),
        errors=len(result.errors), skipped=len(result.skipped), names=identities, sources=sources,
        http_requests=0, model_starts=0)
    pkg.save(out/'result.json', record)
    print(json.dumps({k:v for k,v in record.items() if k not in ('sources', 'names')}))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
