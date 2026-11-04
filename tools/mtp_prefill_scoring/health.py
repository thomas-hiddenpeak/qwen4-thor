"""Accepted installed scoring release: two heads, three cache requests each."""
import json
from pathlib import Path
import signal
import time
import urllib.request

import driver

prior = driver.module('scoring_health_cases', 'tools/session_incremental/health.py')


def validate(plan):
    if (plan['stage'] != 'SCORING_HEALTH' or plan['http_requests'] != 6 or
            plan['http_loads'] != 2 or len(plan['steps']) != 2 or
            [s['head'] for s in plan['steps']] != ['full', 'subset'] or
            any(s['http_requests'] != 3 or s['http_loads'] != 1
                for s in plan['steps'])):
        raise ValueError('health budget')
    if not 0 < plan['stage_timeout_seconds'] <= 900 or time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('fifteen-minute health deadline')
    decision = json.loads(Path(plan['required_gate']).read_text())
    if (decision['status'] != 'PASS_SCORING_ENGINEERING_ACCEPTANCE' or
            decision['candidate_sha256'] != plan['candidate_sha256'] or
            not decision['passed']):
        raise ValueError('accepted candidate required')


def lifecycle(plan, out, check):
    for name, route in [('capabilities', '/v1/capabilities'),
                        ('configuration', '/v1/config')]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{plan['port']}" + route, timeout=10) as response:
            body = response.read()
        json.loads(body)
        (out / (name + '.json')).write_bytes(body)
    step = json.loads((out / 'step.json').read_text())
    # Default snapshot behavior; do not enable the older incremental experiment.
    prior.case(plan, step, None, out, None, check)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned scoring health interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    driver.lifecycle = lifecycle
    driver.reuse.validate = validate
    driver.reuse.custom_step = driver.custom_step
    driver.reuse.admission.audit_server = driver.audit
    driver.reuse.main()
