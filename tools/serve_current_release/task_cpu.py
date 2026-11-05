"""Grouped exact-version application and driver CPU checks, without HTTP/GPU."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_current_release import task_stage as s
from tools.project_tasks import test_tasks
from tools.serve_current_release import test_tasks as current_tests
from tools.project_tasks import protocol_driver_cpu


def main():
    os.umask(0o077);s.configure()
    out=s.BASE/'cpu';out.mkdir(mode=0o700)
    sources={n:s.pkg.sha(ROOT/n) for n in s.CPU_SOURCES}
    for name in sources:
        path=out/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    test_tasks.BASE=s.BASE
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
        for module in (current_tests,protocol_driver_cpu))
    def ids(value):
        return [n for child in value for n in ids(child)] if isinstance(value,unittest.TestSuite) else [value.id()]
    names=ids(suite);s.pkg.require(len(names)==len(set(names)),'duplicate CPU checks')
    log=io.StringIO()
    with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    passed=result.wasSuccessful() and not result.skipped and all(s.pkg.sha(ROOT/n)==v for n,v in sources.items())
    kills=[json.loads(p.read_bytes()) for p in sorted((s.BASE/'fixtures').glob('*/killed-*.json'))]
    s.pkg.save(out/'result.json',dict(passed=passed,tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
        names=names,sources=sources,file_operation_kills=kills,models_started=0,listeners_started=0,gpu_started=0))
    print(json.dumps(dict(passed=passed,tests=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),file_operation_kills=len(kills))))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
