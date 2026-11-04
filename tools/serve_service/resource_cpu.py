"""Freeze the affected host envelope/managed-package CPU contracts as one group."""
import ast
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import package as pkg,test_service,test_memory,test_controller_death,test_inspection,test_resources
from tools.serve_gateway import managed,test_gateway,test_managed
from tools.serve_clients import test_packages
from tools.serve_retention import test_retention

BASE=ROOT/'.q4t-work/serve-host-envelope-20261104'
MODULES=(test_service,test_memory,test_controller_death,test_inspection,test_resources,
         test_gateway,test_managed,test_packages,test_retention)
SOURCES=sorted(set((*pkg.V5_SOURCES,*managed.V3_SOURCES,
    *(str(Path(m.__file__).relative_to(ROOT)) for m in MODULES),
    'tools/serve_service/resource_cpu.py','tools/serve_service/resource_kernel.py',
    'tools/serve_service/resource_worker.py','tools/serve_service/resource_payload.py',
    'tools/serve_retention/runner.py','tools/serve_retention/store.py','tools/serve_retention/exec_managed.py',
    'docs/SERVE_HOST_ENVELOPE_2026-11-04.md')))


def run():
    os.umask(0o077)
    output=BASE/'cpu-01';output.mkdir(mode=0o700)
    sources={n:pkg.sha(ROOT/n) for n in SOURCES}
    for name in sources:
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
        if name.endswith('.py'):ast.parse(path.read_text(),filename=name)
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in MODULES)
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),sources=sources,
        models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run())
