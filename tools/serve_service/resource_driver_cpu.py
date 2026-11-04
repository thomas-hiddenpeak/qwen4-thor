"""Host-envelope driver cleanup and environmental abort contracts, no GPU."""
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resource_stage as stage,package as pkg
from tools.project_tasks import test_stage


def run():
    os.umask(0o077)
    output=stage.BASE/'cpu-driver';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in stage.SOURCES}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO()
    with patch.object(test_stage,'stage',stage):
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_stage))
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,failures=len(result.failures),
        errors=len(result.errors),sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(run())
