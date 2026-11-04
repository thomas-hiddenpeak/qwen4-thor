"""Recheck only the driver's corrected exception assertion, no HTTP/model."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import tls_stage as s,tls_driver_cpu as tests


def main():
    os.umask(0o077);output=s.BASE/'cpu-driver-fix';output.mkdir(mode=0o700)
    sources={name:s.pkg.sha(ROOT/name) for name in s.SOURCES}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();test=tests.Driver('test_formal_cleanup_failure_cannot_keep_pass_and_still_stops_backend')
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite([test]))
    (output/'tests.log').write_text(log.getvalue())
    s.pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,models_started=0,http_started=0,gpu_started=0,
        original_cpu_sha256=s.pkg.sha(s.BASE/'cpu-driver/result.json')))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
