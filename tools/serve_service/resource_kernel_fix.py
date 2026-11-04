"""Preserve the throttled high probe; only finish its affected/unrun cases."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resource_cpu as cpu,resource_kernel as kernel,resource_worker as worker
from tools.serve_service import package as pkg

BASE=cpu.BASE
CHANGED={'tools/serve_service/resource_kernel.py','tools/serve_service/resource_worker.py'}


class Correction(unittest.TestCase):
    def test_observed_high_pressure_completes_without_waiting_for_allocation(self):
        failed=BASE/'kernel-01/high'
        value=pkg.read(failed/'resources-final.json')['final']
        self.assertGreater(value['memory.events']['high'],0)
        self.assertFalse((failed/'payload-held.json').exists())
        with patch.object(worker.r,'sample',return_value=value):
            self.assertTrue(worker.completed(failed,'high'))
        with patch.object(worker.r,'sample',return_value={'memory.events':dict(high=0)}):
            self.assertFalse(worker.completed(failed,'high'))
        self.assertFalse(worker.completed(failed,'oom'))


def gate():
    first=pkg.read(BASE/'cpu-01/result.json')
    pkg.require(first['passed'] and first['tests_run']==155,'original CPU group')
    for name,checksum in first['sources'].items():
        if name not in CHANGED:pkg.require(pkg.sha(ROOT/name)==checksum,'accepted source changed')
    pkg.require(pkg.read(BASE/'kernel-01/normal/result.json')['passed'],'normal evidence')
    pkg.require('TimeoutExpired' in pkg.read(BASE/'kernel-01/high/failure.json')['error'] and
        pkg.read(BASE/'kernel-01/high/failure-cleanup.json')['scope_removed'],'preserved throttling failure')
    return first


def test():
    first=gate();output=BASE/'cpu-fix';output.mkdir(mode=0o700)
    names=sorted(set(first['sources'])|{'tools/serve_service/resource_kernel_fix.py'})
    sources={n:pkg.sha(ROOT/n) for n in names}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Correction))
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),sources=sources,
        reused_cpu_sha256=pkg.sha(BASE/'cpu-01/result.json'),models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('test','run'))
    if parser.parse_args().action=='test':raise SystemExit(test())
    gate()
    kernel.run(cases=('high','oom','tasks','drift','death'),output=BASE/'kernel-02',tested_path=BASE/'cpu-fix/result.json')
