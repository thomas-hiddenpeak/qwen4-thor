"""Offline D integrity and timing disclosure; no inferred loss tolerance."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/prefix-seed-20261028'


def main():
    gate = json.loads((BASE / 'gate-d.json').read_text())
    plan = json.loads((BASE / 'phase-d/plan.json').read_text())
    if not gate['passed'] or len(gate['results']) != 15:
        raise ValueError('Complete five-tier output gate required')
    services, arms = [], {}
    for step in plan['steps']:
        folder = BASE / 'phase-d/run' / step['name']
        raw = (folder / 'server.log').read_text().splitlines()
        shared = [line for line in raw if line.startswith('[prefix-cache] request=')]
        session = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw
                   if line.startswith('[session-cache] request=')]
        if shared or any(r['event'] == 'save' or int(r['prefix']) > 0
                         for r in session):
            raise ValueError('Protection unexpectedly reused or saved a snapshot')
        services.append(dict(name=step['name'], snapshot_saves=0,
                             snapshot_restored_tokens=0))
        arms[step['head'], step['binary'] == plan['candidate']] = {
            c['id']: c for c in json.loads((folder / 'results.json').read_text())}
    pairs = []
    for head in ['full', 'subset', 'plain']:
        for tier in plan['protection']:
            off = arms[head, False][tier['id']]['rows']
            on = arms[head, True][tier['id']]['rows']
            if len(off) != 3 or len(on) != 3:
                raise ValueError('Frozen three-request tier count')
            for index, (a, b) in enumerate(zip(off, on)):
                if any(a[key] != b[key] for key in [
                        'prompt_sha256', 'input_tokens', 'output_sha256',
                        'output_tokens', 'finish']):
                    raise ValueError('Paired input/output contract differs')
                pairs.append(dict(head=head, tier=tier['id'], index=index,
                    input_tokens=a['input_tokens'], output_tokens=a['output_tokens'],
                    ttft_off=a['ttft'], ttft_on=b['ttft'],
                    latency_off=a['latency'], latency_on=b['latency']))
    if not all(r['output_same'] for r in gate['results']):
        raise ValueError('Prompt/output set integrity')
    negatives = [dict(head=r['head'], tier=r['tier'],
                     e2e_percent=r['e2e_percent'],
                     decode_percent=r['decode_percent'],
                     ttft_percent=r['ttft_percent']) for r in gate['results']
                 if r['e2e_percent'] < 0 or r['decode_percent'] < 0
                 or r['ttft_percent'] > 0]
    result = dict(integrity_passed=True, paired_requests=len(pairs),
        services=services, pairs=pairs, negative_observations=negatives,
        all_tier_metrics=gate['results'],
        timing_acceptance='REQUIRES_ENGINEERING_REVIEW',
        statistical_noninferiority='NOT_ESTABLISHED',
        note='Frozen descriptive sample only. Negative values are retained; '
             'this tool applies no percentage loss allowance and makes no '
             'causal regression finding.')
    (BASE / 'protection-review.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(paired_requests=len(pairs),
                         negative_observations=negatives), indent=2))


if __name__ == '__main__':
    main()
