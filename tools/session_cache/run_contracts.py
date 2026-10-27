"""Full-phase lease/monitor driver for the two session state references."""
import json
import time
from pathlib import Path

from run_stage import reuse, legacy


def validate(plan):
    if (plan['stage'] != 'SESSION_B' or plan['http_requests'] != 0 or
            plan['http_loads'] != 0 or len(plan['steps']) != 2):
        raise ValueError('frozen two-head GPU contract scope')
    if plan['stage_timeout_seconds'] > 1800 or time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('contract wall/deadline')
    gate = json.loads(Path(plan['first_http_gate']).read_text())
    if not gate['passed'] or gate['candidate_sha256'] != plan['candidate_sha256']:
        raise ValueError('HTTP gate identity')


if __name__ == '__main__':
    reuse.validate = validate
    reuse.main()
