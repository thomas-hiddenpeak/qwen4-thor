"""Preserve the complete CPU group before any managed gateway HTTP run."""
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed as m

BASE=ROOT/'.q4t-work/serve-gateway-managed-20261104'
CPU=BASE/'cpu'
SOURCES=(*m.SOURCES,'tools/serve_gateway/test_managed.py',
    'tools/serve_gateway/managed_cpu.py','tools/serve_gateway/test_gateway.py',
    'docs/SERVE_GATEWAY_MANAGED_2026-11-04.md')


def run():
    BASE.mkdir(exist_ok=True,mode=0o700);CPU.mkdir(mode=0o700)
    for name in SOURCES:
        dest=CPU/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        m.g.save(dest,(ROOT/name).read_bytes())
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName('tools.serve_gateway.test_managed'))
    m.g.save(CPU/'tests.log',stream.getvalue().encode())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),sources={n:m.pkg.sha(ROOT/n) for n in SOURCES},
        model_or_http_or_gpu_started=False)
    m.pkg.save(CPU/'result.json',value);print(stream.getvalue(),end='')
    m.g.require(value['passed'],'CPU failed; retain original evidence and correct affected cases')
    return {k:v for k,v in value.items() if k!='sources'}


if __name__=='__main__':print(json.dumps(run()))
