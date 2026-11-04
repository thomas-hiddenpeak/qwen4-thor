"""Retry only official rule timelines after unsupported NaNx80 fixture syntax."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import cpu


def run():
    os.umask(0o077);output=cpu.BASE/'cpu-rule-fix';output.mkdir(mode=0o700)
    sources={n:cpu.pkg.sha(ROOT/n) for n in (*cpu.SOURCES,'tools/serve_tls/cpu_fix.py')}
    for name in sources:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
    fixture=cpu.rule_tests();cpu.pkg.save(output/'rule-tests.json',fixture)
    cpu.pkg.require(cpu.pkg.sha(cpu.PROMTOOL)==cpu.n.BINARIES['promtool'],'promtool identity')
    result=subprocess.run([cpu.PROMTOOL,'test','rules',output/'rule-tests.json'],capture_output=True,timeout=45)
    (output/'series.stdout').write_bytes(result.stdout);(output/'series.stderr').write_bytes(result.stderr)
    cpu.pkg.save(output/'result.json',dict(passed=result.returncode==0,returncode=result.returncode,rule_scenarios=len(fixture['tests']),
        sources=sources,original_cpu_sha256=cpu.pkg.sha(cpu.BASE/'cpu-01/result.json'),models_started=0,http_started=0,gpu_started=0))
    print((result.stdout+result.stderr).decode(),end='');return result.returncode


if __name__=='__main__':raise SystemExit(run())
