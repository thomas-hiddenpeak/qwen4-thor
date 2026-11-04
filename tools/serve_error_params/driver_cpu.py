"""Run the owned-process/abort/verification driver safeguards."""
import io
import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_error_params import cpu,test_stage


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--round',default='driver-cpu')
    name=parser.parse_args().round
    cpu.require(name in ('driver-cpu','driver-cpu-v2','driver-cpu-v3'),'unknown driver round')
    out=cpu.BASE/name;out.mkdir()
    paths=['tools/serve_error_params/'+name for name in
           ('stage.py','driver_cpu.py','test_stage.py')]
    frozen={name:cpu.sha(ROOT/name) for name in paths}
    with (out/'tests.log').open('x') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(test_stage.Driver))
    passed=result.wasSuccessful() and result.testsRun==4 and all(cpu.sha(ROOT/p)==v for p,v in frozen.items())
    cpu.save(out/'result.json',dict(passed=passed,tests=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),sources=frozen,http=0,gpu=0,models=0))
    print(dict(passed=passed,tests=result.testsRun))
    cpu.require(passed,'driver CPU failed; preserve log')


if __name__=='__main__':main()
