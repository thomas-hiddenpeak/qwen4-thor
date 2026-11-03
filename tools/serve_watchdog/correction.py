"""Recheck only the missing metric HELP/TYPE correction; keep CPU originals."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_watchdog import cpu, core as c, watchdog as w
from tools.serve_watchdog.storage import encoded


def run():
    original = cpu.BASE/'cpu'
    result = c.parse((original/'result.json').read_bytes())
    c.require(result['tests_run'] == 25 and result['failures'] == 0 and
              result['errors'] == 0 and not result['passed'], 'original_CPU')
    for index in (0, 1, 3):
        c.require(c.parse((original/f'check-{index}.json').read_bytes())[
            'returncode'] == 0, 'unchanged_CPU_failed')
    c.require(c.parse((original/'check-2.json').read_bytes())['returncode']
              == 3, 'original_metric_failure')
    for name, digest in result['sources'].items():
        if name != 'tools/serve_watchdog/core.py':
            c.require(w.file_sha(ROOT/name) == digest, 'unrelated_source_change')
    before = (original/'source/tools/serve_watchdog/core.py').read_text()
    after = (ROOT/'tools/serve_watchdog/core.py').read_text()
    c.require(before.split('\ndef metrics(')[0] ==
              after.split('\ndef metrics(')[0], 'nonmetric_source_change')
    output = cpu.BASE/'cpu-correction'
    output.mkdir(mode=0o700)
    (output/'core.py').write_text(after)
    log = io.StringIO()
    tests = unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_watchdog.test_watchdog.HeartbeatTests.'
        'test_metrics_are_bounded_and_no_credential_or_payload')
    checked = unittest.TextTestRunner(stream=log, verbosity=2).run(tests)
    (output/'tests.log').write_text(log.getvalue())
    c.require(w.file_sha(cpu.PROMTOOL) == cpu.PROMTOOL_SHA, 'promtool_identity')
    metrics = c.metrics(c.status(c.new_state('a'*64, w.now()), w.now()), 'thor-test')
    proc = subprocess.run([cpu.PROMTOOL, 'check', 'metrics'], input=metrics,
                          text=True, capture_output=True, timeout=10)
    (output/'metrics.prom').write_text(metrics)
    (output/'metrics-check.json').write_bytes(encoded(dict(
        returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)))
    sources = {name:w.file_sha(ROOT/name) for name in cpu.SOURCES}
    value = dict(passed=checked.wasSuccessful() and proc.returncode == 0,
        targeted_tests=checked.testsRun, effective_cpu_tests=25,
        official_rule_scenarios_reused=11, initial_cpu_preserved=True,
        original_result_sha256=w.file_sha(original/'result.json'),
        source_change='Only metric HELP/TYPE exposition', sources=sources,
        model_gpu_http_started=False)
    (output/'result.json').write_bytes(encoded(value))
    print(log.getvalue()); print(json.dumps({k:v for k,v in value.items() if k != 'sources'}))
    c.require(value['passed'], 'targeted_metrics_failed')


if __name__ == '__main__': run()
