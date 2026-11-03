"""Targeted first-arrival clock and read-only inspection correction."""
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_watchdog import core as c, watchdog as w, cpu
from tools.serve_watchdog.storage import encoded

TESTS = [
    'HeartbeatTests.test_regressed_boottime_unknown_and_receive_refused',
    'HeartbeatTests.test_receiver_reboot_unknown_until_new_authenticated_delivery',
    'StorageTests.test_claim_content_and_other_process_exclusion',
    'StorageTests.test_status_is_readonly_even_without_claim_files',
    'StorageTests.test_readonly_status_does_not_reset_expiry_and_watcher_restart',
    'StorageTests.test_durable_write_failure_keeps_original_and_pending_file',
]


def run():
    output = cpu.BASE/'review-cpu'; output.mkdir(mode=0o700)
    sources = {name:w.file_sha(ROOT/name) for name in cpu.SOURCES}
    for name in cpu.SOURCES:
        dest = output/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames([
            'tools.serve_watchdog.test_watchdog.'+name for name in TESTS]))
    (output/'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        tests=TESTS, failures=len(result.failures), errors=len(result.errors),
        effective_cpu_tests=26, official_rules_reused=11,
        reused_cpu='cpu/result.json', reused_metrics='cpu-correction/result.json',
        review_reproducer='review/regression-before-first-receipt.json',
        source_scope='Receive initial clock guard and inspect read-only path',
        sources=sources, model_gpu_http_started=False)
    (output/'result.json').write_bytes(encoded(value))
    print(log.getvalue()); print(json.dumps({k:v for k,v in value.items() if k != 'sources'}))
    c.require(value['passed'], 'review_CPU_failed')


if __name__ == '__main__': run()
