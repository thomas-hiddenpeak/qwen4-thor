"""Targeted driver correction only; no repeated model or application suite."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.project_tasks import error_finish as f,test_error_finish,test_tasks,store


def run():
    os.umask(0o077)
    output=f.BASE/"cpu-finish";output.mkdir(mode=0o700)
    sources={name:f.s.pkg.sha(ROOT/name) for name in f.SOURCES}
    for name in sources:
        path=output/"source"/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    test_tasks.BASE=f.BASE
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_error_finish))
    (output/"tests.log").write_text(log.getvalue())
    store.write_new(output/"result.json",store.encode(dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),sources=sources,models_started=0,listeners_started=0,gpu_started=0)))
    print(log.getvalue(),end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
