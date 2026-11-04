"""Preserve the original fixture failure; rerun only its historical-ledger check."""
import io
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True; sys.path.insert(0,str(ROOT))
from tools.project_tasks import protocol_cpu as cpu, test_protocol, test_tasks
from tools.serve_service import package as pkg

CHANGED="tools/project_tasks/test_protocol.py"
TARGET="test_seven_historical_source_snapshots_and_actual_11g_final_ledger"


def gate():
    original=pkg.read(cpu.BASE/"cpu-01/result.json")
    pkg.require(original["tests_run"]==140 and original["errors"]==1 and original["failures"]==0 and not original["passed"],"original CPU receipt")
    log=(cpu.BASE/"cpu-01/tests.log").read_text()
    pkg.require("ERROR: "+TARGET in log and "KeyError: 'previous'" in log,"preserved fixture failure")
    for name,digest in original["sources"].items():
        if name!=CHANGED: pkg.require(pkg.sha(ROOT/name)==digest,"unaffected CPU source changed: "+name)
    return original


def main():
    os.umask(0o077); gate(); output=cpu.BASE/"cpu-history-fix"; output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in (*cpu.SOURCES,"tools/project_tasks/protocol_fix_cpu.py")}
    for name in sources:
        path=output/"source"/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes((ROOT/name).read_bytes())
    test_tasks.BASE=cpu.BASE
    log=io.StringIO(); result=unittest.TextTestRunner(stream=log,verbosity=2).run(test_protocol.Protocol(TARGET))
    (output/"tests.log").write_text(log.getvalue())
    pkg.save(output/"result.json",dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),failures=len(result.failures),
        original_sha256=pkg.sha(cpu.BASE/"cpu-01/result.json"),sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end=""); return 0 if result.wasSuccessful() else 1


if __name__=="__main__": raise SystemExit(main())
