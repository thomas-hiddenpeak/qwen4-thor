"""Execute only the two original unstarted subset C services."""
import json
from pathlib import Path
import time

import run_performance as common
from run_stage import reuse
from audit import audit_server


def validate(plan):
    if (plan['stage'] != 'SESSION_C' or plan['http_requests'] != 48 or
        plan['http_loads'] != 2 or len(plan['steps']) != 2 or
        [s['head'] for s in plan['steps']] != ['subset','subset'] or
        [s['cache'] for s in plan['steps']] != [True,False] or
        any(s['http_requests'] != 24 for s in plan['steps'])):
        raise ValueError('original remaining subset ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('original deadline')
    for path in plan['required_gates']:
        if not json.loads(Path(path).read_text())['passed']:
            raise ValueError('unpassed CPU/prior gate')


if __name__ == '__main__':
    reuse.validate = validate
    reuse.custom_step = common.custom_step
    reuse.admission.audit_server = audit_server
    reuse.main()
