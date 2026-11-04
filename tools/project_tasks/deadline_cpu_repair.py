"""Recheck only the two failed fixtures, retaining the original CPU result."""
import ast
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
from tools.project_tasks import deadline_cpu as cpu, test_transport_deadline as test
from tools.project_tasks import test_trust, transport, store
from tools.serve_service import package as pkg


class MaterialChange(test_trust.Materials):
    def test_forced_metadata_change_during_ca_open_is_rejected(self):
        original = os.open
        injected = []
        def opened(path, *args, **kwargs):
            fd = original(path, *args, **kwargs)
            if Path(path) == self.ca:
                before = self.ca.stat()
                self.ca.write_bytes(self.authorities[1][0].read_bytes())
                # Creation and rewrite can fall within one filesystem clock
                # tick. Inject an observable metadata change deterministically.
                os.utime(self.ca, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                self.assertNotEqual(before.st_mtime_ns, self.ca.stat().st_mtime_ns)
                injected.append(True)
            return fd
        with patch.object(transport.os, 'open', side_effect=opened), \
                self.assertRaises(store.ContractError):
            transport.profile(self.path)
        self.assertEqual(injected, [True])


def prior():
    record = pkg.read(cpu.BASE/'cpu/result.json')
    pkg.require(record['tests_run'] == 16 and record['failures'] == 2 and
        not record['errors'] and not record['skipped'], 'original CPU outcomes')
    changed = 'tools/project_tasks/test_transport_deadline.py'
    for name, digest in record['sources'].items():
        if name != changed:
            pkg.require(pkg.sha(ROOT/name) == digest, 'unaffected CPU source changed: '+name)
    trees = [ast.parse(path.read_text()) for path in
             (cpu.BASE/'cpu/source'/changed, ROOT/changed)]
    for tree in trees:
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Deadlines')
        cls.body = [n for n in cls.body if n.name != 'test_body_drips_and_error_body_use_same_deadline']
    pkg.require(ast.dump(trees[0]) == ast.dump(trees[1]), 'unaffected test AST changed')
    return record


def main():
    os.umask(0o077)
    record = prior()
    out = cpu.BASE/'cpu-repair'
    out.mkdir(mode=0o700)
    test_trust.BASE = out
    names = (*record['sources'], 'tools/project_tasks/deadline_cpu_repair.py')
    sources = {name:pkg.sha(ROOT/name) for name in names}
    for name in names:
        target = out/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    suite = unittest.TestSuite((test.Deadlines('test_body_drips_and_error_body_use_same_deadline'),
        MaterialChange('test_forced_metadata_change_during_ca_open_is_rejected')))
    identities = [item.id() for item in suite]
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    (out/'tests.log').write_text(output.getvalue())
    receipt = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        names=identities, sources=sources, original_sha256=pkg.sha(cpu.BASE/'cpu/result.json'),
        unaffected_passes_reused=14, effective_tests=16, model_starts=0, http_requests=0)
    pkg.save(out/'result.json', receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('sources', 'names')}))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
