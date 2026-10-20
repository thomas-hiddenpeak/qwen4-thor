"""Verify sealed warm-start observations without running GPU work."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/warm-start-20261020'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def allocations(path):
    pending, finished = {}, []
    gu_index = 0
    for line in path.read_text().splitlines():
        match = re.fullmatch(
            r'\[alloc-observer\] (\w+) (begin|end) id=(\d+) bytes=(\d+) '
            r'mono=([\d.]+) caller_offset=(\d+) result=(-?\d+)', line)
        if not match:
            continue
        api, phase, ident, size, stamp, caller, result = match.groups()
        row = dict(api=api, id=int(ident), bytes=int(size), mono=float(stamp),
                   caller_offset=int(caller), result=int(result))
        if phase == 'begin':
            assert ident not in pending
            if api == 'cudaMalloc' and int(size) == 419430400:
                row['gu_index'] = gu_index
                gu_index += 1
            pending[ident] = row
        else:
            begin = pending.pop(ident)
            assert all(begin[k] == row[k] for k in
                       ('api', 'id', 'bytes', 'caller_offset'))
            finished.append(dict(**begin, end=row['mono'],
                duration=row['mono'] - begin['mono'], returncode=row['result']))
    assert finished
    return sorted(finished, key=lambda r: r['duration'], reverse=True), pending


def main():
    identity = read(BASE / 'build-identity.json')
    expected = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
    assert identity['default_sha256'] == identity['unwrapped_sha256'] == expected
    assert digest(ROOT / 'build/q4t') == expected
    assert digest(BASE / 'q4t') == identity['observer_sha256']
    results = []
    for name in ('diagnostic-v1', 'diagnostic-v2'):
        stage = BASE / name
        sealed = read(stage / 'sealed-files.json')
        for rel, sha in sealed.items():
            assert digest(stage / rel) == sha, rel
        plan = read(stage / 'run/plan.json')
        assert plan['binary_sha256'] == identity['observer_sha256']
        rows, pending = allocations(stage / 'run/normal/http/server.log')
        slow = rows[0]
        witnesses = []
        for line in (stage / 'run/stacks.jsonl').read_text().splitlines():
            sample = json.loads(line)
            if not slow['mono'] <= sample['monotonic'] <= slow['end']:
                continue
            kinds = set()
            for target in sample['targets']:
                stack = '\n'.join(t['stack']['value'] or '' for t in target['tasks'])
                if target['kind'] == 'model' and 'compact' in stack:
                    kinds.add('model')
                if target['kind'] == 'nvml' and 'rmapiLockAcquire' in stack:
                    kinds.add('nvml')
            if kinds == {'model', 'nvml'}:
                witnesses.append(sample['monotonic'])
        symbol = subprocess.check_output(['addr2line', '-Cf', '-e',
            str(BASE / 'q4t'), hex(slow['caller_offset'])], text=True).strip()
        pending_evidence = []
        for allocation in pending.values():
            simultaneous = []
            last = allocation['mono']
            for line in (stage / 'run/stacks.jsonl').read_text().splitlines():
                sample = json.loads(line)
                if sample['monotonic'] < allocation['mono']:
                    continue
                last = sample['monotonic']
                kinds = set()
                for target in sample['targets']:
                    stack = '\n'.join(t['stack']['value'] or '' for t in target['tasks'])
                    if target['kind'] == 'model' and 'compact' in stack:
                        kinds.add('model')
                    if target['kind'] == 'nvml' and 'rmapiLockAcquire' in stack:
                        kinds.add('nvml')
                if kinds == {'model', 'nvml'}:
                    simultaneous.append(sample['monotonic'])
            pending_evidence.append(dict(allocation=allocation,
                observer_end=last, simultaneous_samples=simultaneous,
                duration_not_measured_without_end_record=True,
                caller=subprocess.check_output(['addr2line', '-Cf', '-e',
                    str(BASE/'q4t'), hex(allocation['caller_offset'])],
                    text=True).strip()))
        results.append(dict(stage=name, seal=read(stage/'seal.json'),
            sealed_files=len(sealed), completed_allocations=len(rows),
            unfinished_allocations=pending, pending_evidence=pending_evidence, longest_allocations=rows[:5],
            simultaneous_compaction_lock_samples=witnesses,
            slowest_caller=symbol))
    assert results[0]['seal']['guard_status'] == 'ENV_ABORT'
    assert results[0]['longest_allocations'][0]['duration'] > 5
    assert results[0]['simultaneous_compaction_lock_samples']
    assert results[1]['seal']['guard_status'] == 'ENV_ABORT'
    assert any(p['allocation']['bytes'] == 2348023808 and
               p['simultaneous_samples'] for p in results[1]['pending_evidence'])
    preparation = read(BASE / 'diagnostic-v2/run/partial-cache-preparation.json')
    remaining = read(BASE / 'diagnostic-v2/run/cache-after-partial-reclaim.json')
    assert not preparation['preparation'][0]
    assert preparation['preparation'][2][-1]['payload_resident_bytes'] == 0
    assert remaining['complete_file_set_observed']
    remaining_bytes = sum(f['resident_bytes'] for f in remaining['files'])
    assert remaining_bytes >= 32 << 30
    report = dict(partial_reclaim_bytes=preparation['bytes'],
                  remaining_model_cache_bytes=remaining_bytes, status='VERIFIED_DIAGNOSTIC_EVIDENCE', identity=identity,
                  results=results, warm_start_fixed=False,
                  performance_acceptance=False,
                  driver_lock_owner_proven=False)
    (BASE / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('status', 'warm_start_fixed')}))


if __name__ == '__main__':
    main()
