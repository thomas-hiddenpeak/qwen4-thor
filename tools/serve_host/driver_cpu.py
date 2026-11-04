"""Freeze and check the short driver without starting a listener or GPU."""
import ast
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import stage,test_stage
from tools.serve_service import package as pkg


def run(name='cpu-driver'):
    os.umask(0o077);pkg.require(name in ('cpu-driver','cpu-driver-02'),'bounded driver receipt')
    output=stage.BASE/name;output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in stage.SOURCES}
    for name in sources:
        raw=(ROOT/name).read_bytes();path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        if name.endswith('.py'):ast.parse(raw,filename=name)
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_stage))
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),sources=sources,
        models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run(sys.argv[1] if len(sys.argv)==2 else 'cpu-driver'))
