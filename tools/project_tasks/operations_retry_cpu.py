"""Preserve the pre-work invocation refusal and check only the entry repair."""
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
from tools.project_tasks import operations_stage as s

CHANGED = 'tools/project_tasks/operations_stage.py'


def gate():
    cpu = s.pkg.read(s.BASE/'cpu/result.json')
    s.pkg.require(cpu['passed'] and cpu['tests_run'] == 23, 'original CPU receipt')
    for name, digest in cpu['sources'].items():
        s.pkg.require(s.pkg.sha(s.BASE/'cpu/source'/name) == digest, 'CPU original changed')
        if name != CHANGED:
            s.pkg.require(s.pkg.sha(ROOT/name) == digest, 'unaffected CPU source changed')
    exit = s.pkg.read(s.BASE/'execution/lease/exit.json')
    s.pkg.require(exit['status'] == 'COMMAND_FAILED' and exit['child_returncode'] == 1,
                  'original guard outcome')
    s.pkg.require('no matching deployment guard ancestor' in (s.BASE/'run.stderr').read_text()
                  and not (s.BASE/'execution/begin.json').exists() and
                  not list((s.BASE/'execution').glob('attempt-*.json')), 'original pre-work refusal')
    clean = s.pkg.read(s.BASE/'failed-postflight/result.json')
    s.pkg.require(clean['status'] == 'PASS_FAILED_PREFLIGHT' and clean['lock_released'] and
                  clean['model_starts'] == clean['http_requests'] == clean['identity_records'] == 0,
                  'original resource confirmation')
    names = [s.BASE/n for n in ('plan.json', 'run.stdout', 'run.stderr')]
    for name in ('execution', 'failed-postflight'):
        names += [p for p in (s.BASE/name).rglob('*') if p.is_file()]
    return {str(p.relative_to(s.BASE)):s.pkg.sha(p) for p in sorted(names)}


class Entry(unittest.TestCase):
    def test_guard_and_worker_paths_and_output_are_absolute(self):
        args = s.guard_command()
        self.assertIn(str(ROOT/'tools/evalscope/gpu_exclusive.py'), args)
        self.assertIn(str(ROOT/'tools/project_tasks/operations_stage.py'), args)
        self.assertEqual(args[args.index('--output')+1], str(s.OUT/'lease'))
        self.assertTrue(Path(args[0]).is_absolute())
        self.assertNotEqual(s.OUT, s.BASE/'execution')

    def test_new_entry_checks_plan_before_spawning_anything(self):
        with patch.object(s, 'check', side_effect=RuntimeError('identity drift')), \
             patch.object(s.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'identity drift'):
                s.guarded()
        run.assert_not_called()


if __name__ == '__main__':
    os.umask(0o077)
    original = gate()
    output = s.BASE/'cpu-entry-fix'
    output.mkdir(mode=0o700)
    sources = {n:s.pkg.sha(ROOT/n) for n in (CHANGED,
        'tools/project_tasks/operations_retry_cpu.py', 'tools/project_tasks/operations_report.py')}
    for name in sources:
        dest = output/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Entry))
    (output/'tests.log').write_text(log.getvalue())
    s.pkg.save(output/'result.json', dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        sources=sources, original=original, model_starts=0, http_requests=0))
    print(json.dumps(dict(passed=result.wasSuccessful(), tests=result.testsRun)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
