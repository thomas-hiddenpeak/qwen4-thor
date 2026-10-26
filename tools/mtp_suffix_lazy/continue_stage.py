"""Complete only unexecuted B requests after an explicitly approved fixture repair."""
import hashlib
import json
from pathlib import Path
import signal
import time

import run_stage as stage


def validate(plan):
    amendment = plan['continuation']
    approval = json.loads(Path(amendment['approval']).read_text())
    if approval.get('status') != 'APPROVED_BY_USER':
        raise ValueError('one-time continuation exception is not approved')
    original_path = Path(amendment['original_plan'])
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != amendment['original_plan_sha256']:
        raise ValueError('original freeze changed')
    original = json.loads(original_path.read_text())
    old_state = json.loads(Path(amendment['original_state']).read_text())
    audit = json.loads(Path(amendment['partial_audit']).read_text())
    cpu = json.loads(Path(amendment['cpu_repair']).read_text())
    if (old_state['status'] != 'STOPPED' or old_state['loads_started'] != 1 or
            old_state['completed'] or old_state['monitor_failures'] or
            audit['evidence_validity'] != 'VALID_PARTIAL' or
            audit['actual_completed_generation_requests'] != 11 or
            not all(row['passed'] for row in audit['checks']) or
            len(cpu['results']) != 3 or
            any(row['returncode'] for row in cpu['results'])):
        raise ValueError('partial evidence/fixture repair does not qualify')
    if (plan['candidate_sha256'] != original['candidate_sha256'] or
            plan['task_deadline_unix'] != original['task_deadline_unix'] or
            plan['http_requests'] != 215 or plan['http_loads'] != 10 or
            len(plan['steps']) != 10 or plan['contracts'] or
            plan['stage_timeout_seconds'] + old_state['elapsed_seconds'] > 14400 or
            plan['stage_timeout_seconds'] <= 0 or
            time.time() >= plan['task_deadline_unix']):
        raise ValueError('continuation identity or fixed total budget changed')
    if (approval['combined_b_generation_requests'] != 226 or
            approval['combined_b_starts'] != 11 or
            approval['remaining_health_starts'] != 2 or
            approval['task_deadline_unix'] != plan['task_deadline_unix']):
        raise ValueError('approved bounds do not match continuation')
    for i, (new, old) in enumerate(zip(plan['steps'], original['steps'], strict=True)):
        expected = dict(old)
        # These eleven quality requests are already valid; no sample is rerun.
        if i == 0:
            expected.update(quality=False, http_requests=24)
        expected['command'] = new['command']
        if new != expected:
            raise ValueError('only first quality reuse and driver path may change')
    if sum(s['http_requests'] for s in plan['steps']) != 215:
        raise ValueError('remaining request accounting')


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned continuation driver terminated')
    signal.signal(signal.SIGTERM, interrupted)
    stage.reuse.validate = validate
    stage.reuse.custom_step = stage.custom_step
    stage.reuse.admission.audit_server = stage.audit_server
    stage.reuse.main()
