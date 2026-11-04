"""Freeze the retention group before running CPU and real process contracts."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_retention import test_retention as test, runner as r

BASE=test.BASE
SOURCES=sorted(set((*r.gateway.SOURCES,
    'tools/serve_retention/store.py','tools/serve_retention/runner.py',
    'tools/serve_retention/exec_managed.py','tools/serve_retention/test_retention.py',
    'tools/serve_retention/cpu.py','tools/serve_retention/stage.py',
    'tools/serve_retention/test_stage.py','docs/SERVE_RETENTION_2026-11-04.md')))


def run():
    os.umask(0o077)
    output=BASE/'cpu-01';output.mkdir(parents=True,mode=0o700)
    sources={name:r.pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(test.suite())
    (output/'tests.log').write_text(log.getvalue())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),sources=sources,default={n:r.pkg.sha(ROOT/n) for n in
            ('build/q4t','build/q4t.release.json')},models_started=0,http_started=0,gpu_started=0)
    r.pkg.save(output/'result.json',value);print(log.getvalue(),end='')
    r.require(result.wasSuccessful(),'CPU failed; preserve original and rerun only affected cases')
    return {k:v for k,v in value.items() if k!='sources'}


def finalize():
    os.umask(0o077)
    first=r.pkg.read(BASE/'cpu-01/result.json')
    r.require(first['passed'] and first['tests_run']==20,'original CPU gate')
    changed={'tools/serve_retention/runner.py','tools/serve_retention/test_retention.py',
             'tools/serve_retention/cpu.py','docs/SERVE_RETENTION_2026-11-04.md'}
    for name,digest in first['sources'].items():
        r.require(r.pkg.sha(BASE/'cpu-01/source'/name)==digest,'original source snapshot changed')
        if name not in changed:r.require(r.pkg.sha(ROOT/name)==digest,'reused dependency changed')
    output=BASE/'cpu-accepted';output.mkdir(mode=0o700)
    sources={name:r.pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        compile((ROOT/name).read_text(),name,'exec') if name.endswith('.py') else None
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in (
        'tools.serve_retention.test_retention.Runner.test_run_real_child_stop_and_rotation_seals',
        'tools.serve_retention.test_stage'))
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),effective_tests=21,reused_tests=19,sources=sources,
        original_sha256=r.pkg.sha(BASE/'cpu-01/result.json'),default=first['default'],
        models_started=0,http_started=0,gpu_started=0)
    r.pkg.save(output/'result.json',value);print(log.getvalue(),end='')
    r.require(result.wasSuccessful(),'preserve targeted CPU failure')
    return {k:v for k,v in value.items() if k!='sources'}


def ready():
    os.umask(0o077)
    accepted=r.pkg.read(BASE/'cpu-accepted/result.json')
    failed=r.pkg.read(BASE/'cpu-env-original/result.json')
    r.require(accepted['passed'] and accepted['effective_tests']==21 and not failed['passed'] and
              failed['tests_run']==failed['failures']==1 and failed['errors']==0,'environment reproduction gate')
    changed={'tools/serve_retention/stage.py','tools/serve_retention/test_stage.py','tools/serve_retention/cpu.py'}
    for name,digest in accepted['sources'].items():
        r.require(r.pkg.sha(BASE/'cpu-accepted/source'/name)==digest,'accepted source snapshot changed')
        if name not in changed:r.require(r.pkg.sha(ROOT/name)==digest,'reused source changed')
    output=BASE/'cpu-ready';output.mkdir(mode=0o700)
    sources={name:r.pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName(
            'tools.serve_retention.test_stage.Driver.test_gpu_observation_abort_is_not_a_product_failure_or_launch'))
    (output/'tests.log').write_text(log.getvalue())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),effective_tests=22,reused_tests=21,sources=sources,
        accepted_sha256=r.pkg.sha(BASE/'cpu-accepted/result.json'),failed_sha256=r.pkg.sha(BASE/'cpu-env-original/result.json'),
        default=accepted['default'],models_started=0,http_started=0,gpu_started=0)
    r.pkg.save(output/'result.json',value);print(log.getvalue(),end='')
    r.require(result.wasSuccessful(),'preserve targeted environment failure')
    return {k:v for k,v in value.items() if k!='sources'}


if __name__=='__main__':
    action={():run,('finalize',):finalize,('ready',):ready}.get(tuple(sys.argv[1:]))
    if action is None:raise SystemExit('cpu.py [finalize | ready]')
    print(json.dumps(action()))
