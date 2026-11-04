"""Single grouped CPU gate for the deadline repair and pinned TLS/history."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, test_tasks, test_trust, test_transport_deadline
from tools.serve_service import package as pkg

BASE = ROOT/'.q4t-work/serve-task-deadline-20261104'


def main():
    os.umask(0o077)
    out = BASE/'cpu'
    out.mkdir(mode=0o700)
    test_tasks.BASE = BASE
    test_trust.BASE = BASE
    names = (*engine.RUNTIME_FILES, 'tools/project_tasks/test_transport_deadline.py',
             'tools/project_tasks/test_tasks.py', 'tools/project_tasks/test_trust.py',
             'tools/project_tasks/deadline_cpu.py')
    sources = {name:pkg.sha(ROOT/name) for name in names}
    for name in names:
        target = out/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_transport_deadline),
        unittest.defaultTestLoader.loadTestsFromTestCase(test_trust.Materials)))
    def ids(item):
        return [n for child in item for n in ids(child)] if isinstance(item, unittest.TestSuite) else [item.id()]
    identities = ids(suite)
    pkg.require(len(identities) == len(set(identities)), 'unique CPU tests')
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    (out/'tests.log').write_text(output.getvalue())
    record = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        names=identities, sources=sources, model_starts=0, http_requests=0)
    pkg.save(out/'result.json', record)
    print(json.dumps({k:v for k,v in record.items() if k not in ('sources', 'names')}))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
