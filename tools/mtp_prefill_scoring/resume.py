"""Freeze only incomplete work after the explicit repeat-budget correction."""
import copy
import json
from pathlib import Path
import subprocess
import time

import driver

ROOT = driver.ROOT


def main():
    base = ROOT / '.q4t-work/scoring-continuation'
    receipt = json.loads((base / 'reuse.json').read_text())
    plan = copy.deepcopy(json.loads(
        Path(receipt['original_plan']['path']).read_text()))
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit correction before continuation freeze')
    plan['output'] = str(base / 'run')
    plan['reuse_record'] = str(base / 'reuse.json')
    plan['http_requests'] = 172
    plan['original_candidate_source_commit'] = plan['source_commit']
    plan['tool_source_commit'] = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    plan['stage_timeout_seconds'] = int(plan['task_deadline_unix'] - time.time())
    plan['amendment'] = dict(reason='Inherited 2048-token repeat budget corrected to 256',
        original_admitted=34, valid_completed=26, new_requests=172,
        total_admission_ceiling=206, total_service_starts=11,
        absolute_deadline_unchanged=True)
    for case in plan['suffix_cases']:
        case['tokens'] = 256
    for step in plan['steps']:
        command = step['command']
        command[command.index('--plan') + 1] = str(base / 'plan.json')
        for case in step.get('cases', []):
            if case['id'].startswith('repeat-'):
                case['tokens'] = 256
    first = plan['steps'][0]
    first['cases'] = [c for c in first['cases'] if c['id'].startswith('repeat-')]
    first['http_requests'] = 8
    # Old dependencies remain validated against their pre-edit snapshots.
    # Only explicitly reviewed tools are rebound in this continuation.
    for path in receipt['original_tools']:
        plan['files'][path] = driver.legacy.sha(Path(path))
    for path in [base / 'reuse.json', Path(__file__),
                 Path(__file__).with_name('review.py'),
                 ROOT / 'docs/MTP_PREFILL_ADOPTION_CONTINUATION_2026-11-04.md']:
        plan['files'][str(path)] = driver.legacy.sha(path)
    driver.validate(plan)
    driver.legacy.verify_files(plan)
    driver.save(base / 'plan.json', plan)
    print(json.dumps(dict(requests=172, reused=26, loads=10,
        remaining_seconds=plan['stage_timeout_seconds'],
        deadline=plan['task_deadline_unix'], sha256=driver.legacy.sha(base / 'plan.json'))))


if __name__ == '__main__':
    main()
