"""Target only observer early-child cleanup and the affected pinned package."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import cpu,test_host,test_runtime
from tools.serve_service import package as pkg

SOURCES=sorted(set((*cpu.SOURCES,'tools/serve_host/cpu_fix.py',
    'tools/serve_host/test_runtime.py','tools/serve_host/runtime_cpu.py')))


def run():
    os.umask(0o077);prior=pkg.read(cpu.BASE/'cpu-fix-02/result.json')
    pkg.require(prior['passed'],'existing corrected evidence')
    for name,checksum in prior['sources'].items():
        if name!='tools/serve_host/observer.py':pkg.require(pkg.sha(ROOT/name)==checksum,'unaffected source changed')
    output=cpu.BASE/'cpu-runtime';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
    suite=unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(test_runtime),
        unittest.defaultTestLoader.loadTestsFromName('test_pinned_package_nginx_syntax_read_only_observe_and_exclusive_claim',test_host.Host)))
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,reused_cpu_sha256=pkg.sha(cpu.BASE/'cpu-fix-02/result.json'),
        models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run())
