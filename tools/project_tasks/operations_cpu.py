"""One grouped CPU gate for the exact new protocol registration and driver."""
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
from tools.project_tasks import operations_stage as s, test_tasks
from tools.project_tasks import test_operations_protocol, operations_driver_cpu


def main():
    os.umask(0o077)
    output = s.BASE/'cpu'
    output.mkdir(mode=0o700)
    sources = {name:s.pkg.sha(ROOT/name) for name in s.CPU_SOURCES}
    for name in sources:
        dest = output/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    test_tasks.BASE = s.BASE
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
        for module in (test_operations_protocol, operations_driver_cpu))
    def ids(item):
        return ([n for child in item for n in ids(child)] if isinstance(item, unittest.TestSuite)
                else [item.id()])
    names = ids(suite)
    s.pkg.require(len(names) == len(set(names)), 'duplicate CPU tests')
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    kills = [json.loads(p.read_bytes()) for p in sorted((s.BASE/'fixtures').glob('*/killed-*.json'))]
    s.pkg.save(output/'result.json', dict(passed=result.wasSuccessful(),
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped), names=names, sources=sources,
        file_operation_kills=kills, models_started=0, listeners_started=0, gpu_started=0))
    print(json.dumps(dict(passed=result.wasSuccessful(), tests=result.testsRun,
                          failures=len(result.failures), errors=len(result.errors), kills=len(kills))))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
