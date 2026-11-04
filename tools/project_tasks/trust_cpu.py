"""Grouped application trust, migration, retention, backup and CLI contracts."""
import ast
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.project_tasks import engine,store,test_tasks,test_budget,test_migration,test_archive,test_backup,test_errors,test_trust
from tools.project_tasks import error_cpu,migration_fixtures as f
from tools.serve_service import package as pkg

BASE=test_trust.BASE
SOURCES=sorted(set((*error_cpu.SOURCES,*engine.RUNTIME_FILES,'tools/project_tasks/trust_cpu.py','tools/project_tasks/test_trust.py',
    'tools/serve_gateway/tls_fixture.py','tools/serve_gateway/tls.py','tools/serve_gateway/gateway.py',
    'docs/SERVE_TASK_TRUST_2026-11-04.md')))


def main():
    os.umask(0o077);BASE.mkdir(exist_ok=True);output=BASE/'cpu-01';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        raw=(ROOT/name).read_bytes();path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        if name.endswith('.py'):ast.parse(raw,filename=name)
    test_tasks.BASE=BASE
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module) for module in
        (test_tasks,test_budget,test_migration,test_archive,test_backup,test_errors,test_trust))
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    kills=[json.loads(p.read_bytes()) for p in sorted((BASE/'fixtures').glob('*/killed-*.json'))]
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),failures=len(result.failures),
        sources=sources,file_operation_kills=kills,models_started=0,listeners_started=0,gpu_started=0,
        archives={f.release(i)['evidence']:f.release(i)['archive_sha256'] for i in range(6)}))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
