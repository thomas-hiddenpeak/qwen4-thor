"""Account for seed bytes and observed save intervals after C completes."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/mtp-prefill-release-20261029'


def main():
    phase = BASE / 'phase-c'
    plan = json.loads((phase / 'plan.json').read_text())
    if json.loads((phase / 'run/state.json').read_text())['status'] == 'RUNNING':
        raise ValueError('Completed stage required')
    cases, sizes = [], {}
    for step in plan['steps']:
        if not step['http_loads']:
            continue
        folder = phase / 'run' / step['name']
        raw = (folder / 'server.log').read_text().splitlines()
        records = [dict(re.findall(r'(\w+)=(\S+)', line),
                        pool='shared' if line.startswith('[prefix-cache]') else 'session')
                   for line in raw if line.startswith(
                       ('[prefix-cache] request=', '[session-cache] request='))]
        previous = 0
        for row in records:
            if row['pool'] != 'shared':
                continue
            resident = int(row['bytes'])
            entry = resident - previous
            previous = resident
            prefix = int(row['prefix'])
            if entry <= 0 or row['error'] != 'none':
                raise ValueError('Expected append into bounded non-evicting shared pool')
            if prefix in sizes and sizes[prefix] != entry:
                raise ValueError('Same-prefix snapshot size changed')
            sizes[prefix] = entry
            row['entry_bytes'] = entry
        for case in json.loads((folder / 'results.json').read_text()):
            parent = case['rows'][0]['response_id']
            ids = {r['response_id'] for r in case['rows']}
            rows = [r for r in records if r['request'] in ids]
            writes = [r for r in rows if r['pool'] == 'shared']
            source_saves = [r for r in rows if r['pool'] == 'session'
                            and r['event'] == 'save' and r['request'] == parent]
            child_saves = [r for r in rows if r['pool'] == 'session'
                           and r['event'] == 'save' and r['request'] != parent]
            restores = [r for r in rows if r['pool'] == 'session'
                        and r['event'] == 'restore' and r.get('source') == 'shared'
                        and int(r['prefix']) > 0]
            if (len(writes) != int(step['shared'])
                    or any(r['request'] != parent for r in writes)
                    or len(restores) != 3 * int(step['shared'])):
                raise ValueError('Seed and readonly reuse ledger')
            cases.append(dict(head=step['head'], block=step['block'],
                shared=step['shared'], service=step['name'], scenario=case['id'],
                checkpoint_saves=len(writes),
                checkpoint_bytes_written=sum(r['entry_bytes'] for r in writes),
                checkpoint_bytes_not_reused_in_this_transaction=0,
                checkpoint_save_wall_ms=sum(float(r['ms']) for r in writes),
                parent_session_save_ms=[float(r['ms']) for r in source_saves],
                child_session_save_ms=[float(r['ms']) for r in child_saves],
                shared_restore_ms=[float(r['ms']) for r in restores],
                parent_snapshot_prefix=[int(r['prefix']) for r in source_saves],
                parent_ttft=case['rows'][0]['ttft'],
                parent_latency=case['rows'][0]['latency'],
                transaction_seconds=case['makespan'],
                transaction_output_tokens_per_second=case['output_tokens']/case['makespan']))
    result = dict(cases=cases, entry_bytes=sizes,
        method='Per-save bytes are successive resident-byte differences in '
               'each initially empty shared pool (4 entries, no eviction).',
        limits=['Session save includes model-lock wait; concurrent waits '
                'overlap and cannot be summed as GPU execution time.',
                'SSD caching, GC, thermal and host IO causes are not isolated.',
                'Request latency includes final snapshot save and HTTP end.',
                'One seed and three restores per enabled transaction; no '
                'claim of future reuse for any other workload.'])
    (BASE / 'io-review.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
