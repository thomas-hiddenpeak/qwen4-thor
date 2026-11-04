"""Freeze and run driver-only fault checks, without listeners/models/GPU."""
import ast
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_supervisor_deadlines import stage as s,test_stage


def main():
    os.umask(0o077)
    s.configure()
    out=s.BASE/'driver-cpu';out.mkdir(mode=0o700)
    files=s.sources()
    for path in (ROOT/s.HERE).glob('*.py'):
        ast.parse(path.read_bytes(),filename=str(path))
    suite=unittest.defaultTestLoader.loadTestsFromModule(test_stage)
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    passed=result.wasSuccessful() and not result.skipped and files==s.sources()
    s.cpu.save(out/'result.json',dict(passed=passed,tests_run=result.testsRun,
        sources=files,models=0,http=0,gpu=0))
    print(json.dumps(dict(passed=passed,tests=result.testsRun)))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
