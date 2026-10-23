"""One bounded continuation after the v1 synthetic-log fixture defect.

No inherited request is executed or copied. The second lease spans every
remaining step; the first attempt keeps its STOPPED state and independent seal.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import run_stage as stage


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    args = parser.parse_args()
    path = args.plan.resolve()
    plan = json.loads(path.read_text())
    stage.validate_plan(plan)
    resume = plan['resume']
    for source, digest in resume['bindings'].items():
        if sha(source) != digest:
            raise RuntimeError('inherited evidence changed: ' + source)
    old = json.loads(Path(resume['original_stage'], 'run/state.json').read_text())
    if (old['status'] != 'STOPPED' or old['completed'] !=
            ['quality-full-on', 'host-contracts'] or
            old['requests_completed'] != 11 or old['loads_started'] != 1 or
            resume['used_requests'] != 11 or resume['used_loads'] != 1 or
            [s['name'] for s in plan['steps'][:2]] != old['completed'] or
            any(not s.get('inherited') for s in plan['steps'][:2]) or
            any(s.get('inherited') for s in plan['steps'][2:])):
        raise RuntimeError('continuation is limited to the recorded fixture failure')
    audit = json.loads(Path(resume['partial_audit']).read_text())
    if audit['evidence_validity'] != 'VALID' or audit['measurement_complete']:
        raise RuntimeError('prior attempt does not have valid partial evidence')
    remaining = resume['absolute_deadline_epoch'] - time.time()
    if remaining <= 0:
        raise TimeoutError('original four-hour budget exhausted before continuation')
    execution = dict(plan, steps=plan['steps'][2:], http_requests=239,
                     http_loads=17, stage_timeout_seconds=remaining,
                     direct_contract_timeout_seconds=(
                         1200 - resume['contract_charged_seconds']))
    if (sum(s['http_requests'] for s in execution['steps']) != 239 or
            sum(s['http_loads'] for s in execution['steps']) != 17 or
            execution['steps'][0]['name'] != 'admission-stage-contracts' or
            execution['direct_contract_timeout_seconds'] <= 0):
        raise RuntimeError('remaining budget or one-shot failed-contract retry changed')
    stage.legacy.audit_server = stage.audit_server
    # run_stage checks the supplied full plan's hash while executing this
    # explicit reduced view. Its state records only the new 239/17 physical work.
    stage.legacy.run_stage(execution, path)


if __name__ == '__main__':
    main()
