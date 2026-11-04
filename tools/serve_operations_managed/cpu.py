"""Run the bounded driver's fault checks; no listeners or model execution."""
import ast
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_operations_managed import stage, test_stage


def main():
    os.umask(0o077)
    stage.BASE.mkdir(exist_ok=True)
    out = stage.BASE / 'cpu-01'
    out.mkdir(mode=0o700)
    files = stage.sources()
    for path in (ROOT / stage.HERE).glob('*.py'):
        ast.parse(path.read_bytes(), filename=str(path))
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_stage)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (out / 'tests.log').write_text(log.getvalue())
    stage.pkg.save(out / 'result.json', dict(passed=result.wasSuccessful(),
        tests_run=result.testsRun, sources=files, models_started=0, listeners_started=0))
    print(log.getvalue(), end='')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
