"""Retained focused TLS CPU checks; never starts HTTP or a model."""
import argparse
import ast
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed as m

BASE=ROOT/'.q4t-work/serve-tls-lifecycle-20261104'
TESTS=('tools.serve_gateway.test_gateway','tools.serve_gateway.test_managed',
       'tools.serve_gateway.test_tls','tools.serve_clients.test_packages')
SOURCES=sorted(set((*m.V4_SOURCES,*(v.replace('.','/')+'.py' for v in TESTS),
    'tools/serve_gateway/tls_fixture.py','tools/serve_gateway/tls_cpu.py',
    'docs/SERVE_TLS_LIFECYCLE_2026-11-04.md')))


def run(label,tests):
    os.umask(0o077);BASE.mkdir(exist_ok=True)
    output=BASE/label;output.mkdir(mode=0o700)
    sources={name:m.pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        raw=(ROOT/name).read_bytes();dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
        if name.endswith('.py'):ast.parse(raw,filename=name)
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(tests))
    (output/'tests.log').write_text(log.getvalue())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),tests=tests,sources=sources,models_started=0,http_started=0,gpu_started=0)
    m.pkg.save(output/'result.json',value)
    print(log.getvalue(),end='');return 0 if value['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label',default='cpu-01',choices=('cpu-01','cpu-fix','cpu-fix-02'))
    parser.add_argument('--test',action='append')
    args=parser.parse_args();raise SystemExit(run(args.label,args.test or list(TESTS)))
