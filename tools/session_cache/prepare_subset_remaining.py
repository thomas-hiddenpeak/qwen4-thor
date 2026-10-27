"""Preserve completed full-head data and freeze the original remaining 48."""
import copy
import hashlib
import json
from pathlib import Path
import sys

from run_stage import ROOT, save


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    base = ROOT / '.q4t-work/session-cache-20261027'
    original = base / 'phase-c-fixed-cap/plan.json'
    plan = copy.deepcopy(json.loads(original.read_text()))
    gate = base / 'recovery-adjudication.json'
    if not json.loads(gate.read_text())['passed']:
        raise ValueError('unpassed original-data review')
    target = base / 'phase-c-subset'
    target.mkdir()
    plan.update(output=str(target/'run'), http_requests=48, http_loads=2,
        stage_timeout_seconds=6000, steps=plan['steps'][2:],
        required_gates=[str(base/'correctness-gate.json'),str(gate)],
        completed_full_plan=str(original), completed_full_new_requests=42,
        order=['subset ON','subset OFF'],
        remaining_only='No replay of completed full services. Same prompts, '
        'outputs, head, program, order, deadline and total resource caps.')
    path = target / 'plan.json'
    allowed_changes = {str(ROOT/'tools/session_cache'/name) for name in
                       ['run_performance.py','prepare_performance.py']}
    allowed_changes.add(str(ROOT/'docs/SESSION_CACHE_PLAN_2026-10-27.md'))
    for name, expected in list(plan['files'].items()):
        current = sha(name)
        if current != expected and name not in allowed_changes:
            raise ValueError('unexplained binding change: '+name)
        plan['files'][name] = current
    for step in plan['steps']:
        step['command'] = [sys.executable,'-B',str(ROOT/
            'tools/session_cache/run_subset_remaining.py'),'--plan',str(path),
            '--custom-step',step['name']]
    additions = [original,gate,base/'phase-c-fixed-cap/archive/audit.json',
        base/'evidence/oracle-cpu.log'] + [ROOT/'tools/session_cache'/name for name in
        ['oracle.py','test_oracle.py','adjudicate_recovery.py',
         'prepare_subset_remaining.py','run_subset_remaining.py']]
    for item in additions:
        plan['files'][str(item)] = sha(item)
    save(path,plan)
    print(json.dumps(dict(plan=str(path),sha256=sha(path),requests=48,starts=2,
                         deadline=plan['task_deadline_unix']),indent=2))


if __name__ == '__main__':
    main()
