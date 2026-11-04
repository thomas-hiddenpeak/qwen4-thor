"""Only early-child cleanup and its changed package are rechecked."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import runtime_cpu as previous,test_runtime,test_host
from tools.serve_service import package as pkg


def run():
    os.umask(0o077);base=previous.cpu.BASE;original=pkg.read(base/'cpu-runtime/result.json')
    pkg.require(not original['passed'] and original['tests_run']==5 and original['failures']==1 and original['errors']==0,
                'original runtime failure')
    for name,checksum in original['sources'].items():
        if name!='tools/serve_host/observer.py':pkg.require(pkg.sha(ROOT/name)==checksum,'unaffected source changed')
    output=base/'cpu-runtime-fix';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in (*previous.SOURCES,'tools/serve_host/runtime_fix.py')}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    suite=unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromName('test_identity_failure_after_spawn_still_reaps_child',test_runtime.Runtime),
        unittest.defaultTestLoader.loadTestsFromName('test_pinned_package_nginx_syntax_read_only_observe_and_exclusive_claim',test_host.Host)))
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,reused_cpu_sha256=pkg.sha(base/'cpu-runtime/result.json'),
        models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run())
