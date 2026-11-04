"""Correct only the static exporter's private nginx temporary paths."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import cpu,test_host
from tools.serve_service import package as pkg


def run(output_name='cpu-fix'):
    os.umask(0o077);first=pkg.read(cpu.BASE/'cpu-01/result.json')
    pkg.require(not first['passed'] and first['tests_run']==33 and first['errors']==1 and first['failures']==0 and
        all(v['returncode']==0 for v in first['commands']) and first['rule_scenarios']==22,'original grouped evidence')
    changed={'tools/serve_host/observer.py','tools/serve_host/child.py'}
    for name,checksum in first['sources'].items():
        if name not in changed:pkg.require(pkg.sha(ROOT/name)==checksum,'unaffected source changed')
    pkg.require(output_name in ('cpu-fix','cpu-fix-02'),'bounded correction output')
    output=cpu.BASE/output_name;output.mkdir(mode=0o700)
    sources={n:pkg.sha(ROOT/n) for n in (*cpu.SOURCES,'tools/serve_host/cpu_fix.py')}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    suite=unittest.defaultTestLoader.loadTestsFromName('test_pinned_package_nginx_syntax_read_only_observe_and_exclusive_claim',test_host.Host)
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,reused_cpu_sha256=pkg.sha(cpu.BASE/'cpu-01/result.json'),
        models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run(sys.argv[1] if len(sys.argv)==2 else 'cpu-fix'))
