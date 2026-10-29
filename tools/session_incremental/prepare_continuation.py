"""Freeze the sole wiring repair; retain all original failure evidence."""
import hashlib
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT/'.q4t-work/session-incremental-20261029'
BASE = ORIGINAL/'continuation'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def main():
    BASE.mkdir(exist_ok=True)
    if (BASE/'plan.json').exists():
        raise RuntimeError('Do not overwrite frozen continuation')
    old = read(ORIGINAL/'plan.json')
    # Establish the already completed baseline's own window, without claiming
    # the failed phase's final monitoring barrier passed.
    service = ORIGINAL/'run/00-full-incremental-0'
    events = [json.loads(l) for l in (service/'events.jsonl').read_text().splitlines()]
    start, end = events[0]['monotonic_seconds'], events[-1]['monotonic_seconds']
    rows, after = [], []
    with (ORIGINAL/'run/unified-memory.jsonl').open() as f:
        for line in f:
            r = json.loads(line)
            if start <= r['monotonic'] <= end:
                rows.append(r)
            if end < r['monotonic'] <= end+5:
                after.append(r)
    guard = [json.loads(l) for l in (ORIGINAL/'lease/events.jsonl').read_text().splitlines()]
    conflicts = [r for r in guard if start <= r.get('monotonic',0) <= end and r.get('conflicts')]
    gaps = [b['monotonic']-a['monotonic'] for a,b in zip(rows,rows[1:])]
    swaps = [int(m.group(1))*1024 for r in rows for p in r['processes'].values()
             if Path((p.get('identity',{}).get('argv') or [''])[0]).name == 'q4t'
             for m in [re.search(r'VmSwap:\s*(\d+)', p.get('status',''))] if m]
    raw = (ORIGINAL/'run/01-full-incremental-1/server.log').read_text()
    fields = lambda line: dict(re.findall(r'(\w+)=(\S+)',line))
    excluded_writes = sum(int(fields(l)['disk_bytes']) for l in raw.splitlines()
                          if l.startswith('[session-save-timing] '))
    admitted = sum('event=admitted ' in l for l in raw.splitlines())
    if admitted != 5:
        raise RuntimeError('Unexpected failed-service request ledger')
    passed = (len(rows) == 610 and len(after) == 5 and max(gaps) < 2 and
        all(v['status']=='OK' for r in rows for v in r['source_values'].values()) and
        all(r['exit_pending']['active']==0 for r in after) and not conflicts and
        read(service/'exit.json')['http_output_checks_passed'] and
        read(service/'timing-audit.json')['passed'])
    review = dict(passed=passed, start=start,end=end,samples=len(rows),
        max_gap_seconds=max(gaps), after_exit_samples=len(after), conflicts=conflicts,
        baseline_q4t_swap_bytes=max(swaps,default=0), excluded_service_admitted=admitted,
        excluded_service_write_bytes=excluded_writes,
        budget_deviation=dict(original_http=48,revised_http=53,original_starts=4,
            revised_starts=5,original_cpu_programs=1,revised_cpu_programs=2,
            deadline_unchanged=True,candidate_unchanged=True,acceptance_unchanged=True),
        original_archive=read(ORIGINAL/'archive-receipt.json'))
    (BASE/'baseline-window.json').write_text(json.dumps(review,indent=2)+'\n')
    if not passed:
        raise RuntimeError('Cannot reuse baseline window')
    plan = dict(old)
    plan.update(continuation=True, stage='SESSION_INCREMENTAL_CONTINUATION',
        output=str(BASE/'run'),lease_directory=str(BASE/'lease'),http_requests=36,
        http_loads=3,maximum_http_requests=36,maximum_starts=3,
        stage_timeout_seconds=int(old['task_deadline_unix']-time.time()))
    plan['steps'] = [dict(s) for s in old['steps'] if s['kind']=='block' and
                     s['name']!='00-full-incremental-0']
    for step in plan['steps']:
        step['command'][step['command'].index('--plan')+1]=str(BASE/'plan.json')
    paths = [Path(p) for p in old['files']]
    paths += list((ROOT/'tools/session_incremental').glob('*.py'))
    paths += [ROOT/'docs/SESSION_INCREMENTAL_CONTINUATION_2026-10-29.md',
        BASE/'baseline-window.json',ORIGINAL/'plan.json',ORIGINAL/'decision.json',
        ORIGINAL/'archive-receipt.json',ORIGINAL/'tool-repair-cpu.log',
        ORIGINAL/'dependency-review.json',ORIGINAL/'memory-contract-review.json']
    # Bind the baseline evidence and original exit failure as well as pointers
    # to the independently verified complete original archive.
    paths += [p for p in service.rglob('*') if p.is_file()]
    paths += [ORIGINAL/'run/unified-memory.jsonl',ORIGINAL/'run/telemetry-summary.json',
              ORIGINAL/'postflight.json',ORIGINAL/'lease/exit.json',
              ORIGINAL/'lease/events.jsonl',ORIGINAL/'run/04-cpu-contract/contract.log',
              ORIGINAL/'run/04-cpu-contract/result.json']
    plan['files'] = {str(p):sha(p) for p in paths}
    if sha(Path(plan['candidate'])) != old['candidate_sha256']:
        raise RuntimeError('Candidate changed')
    (BASE/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    identity = dict(candidate_sha256=old['candidate_sha256'],plan_sha256=sha(BASE/'plan.json'),
                    files=len(plan['files']),deadline=old['task_deadline_unix'])
    (BASE/'identity.json').write_text(json.dumps(identity,indent=2)+'\n')
    print(json.dumps(identity,indent=2))


if __name__ == '__main__':
    main()
