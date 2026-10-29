"""Read-only review of the completed C pairs and intentionally stopped tail."""
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'


def read(path):
    return json.loads(Path(path).read_text())


def main():
    phase = BASE/'phase-c'
    state = read(phase/'run/state.json')
    if state['status'] != 'STOPPED':
        raise ValueError('Expected preserved intentional stop')
    names = state['completed']
    if names != ['00-full-off', '01-full-on']:
        raise ValueError('Unexpected completed subset')
    arms, raw, slots = {}, {}, {}
    for name in names:
        folder = phase/'run'/name
        if not read(folder/'exit.json')['http_output_checks_passed']:
            raise ValueError('Completed HTTP output')
        if read(folder/'server-audit.json')['finalization']['status'] != 'PASS':
            raise ValueError('Completed server finalization')
        if not all(read(folder/f)['passed'] for f in ['session-audit.json', 'prefix-audit.json']):
            raise ValueError('Completed lifecycle audit')
        arms[name] = {c['id']: c for c in read(folder/'results.json')}
        lines = (folder/'server.log').read_text().splitlines()
        raw[name] = [dict(re.findall(r'(\w+)=(\S+)', line),
            pool='shared' if line.startswith('[prefix-cache]') else 'session')
            for line in lines if line.startswith(('[prefix-cache] request=', '[session-cache] request='))]
        slots[name] = dict(re.findall(r'\[q4t\]\[slot-admission\] request=(\S+) slot=(\d+) ', '\n'.join(lines)))
    results = []
    for scenario in read(phase/'plan.json')['scenarios']:
        case_id = scenario['id']
        off, on = [arms[n][case_id] for n in names]
        same = all(all(a[k] == b[k] for k in ['prompt_sha256', 'input_tokens',
            'output_sha256', 'output_tokens', 'finish']) for a, b in zip(off['rows'], on['rows']))
        cache = ([r['cached_tokens'] for r in off['rows']] == [0]*4 and
            [r['cached_tokens'] for r in on['rows']] == [0]+[scenario['prefix']]*3 and
            all(r['cache_source'] == 'shared' for r in on['rows'][1:]))
        details = []
        for name, case in zip(names, [off, on]):
            rows = case['rows']
            ids = {r['response_id'] for r in rows}
            entries = [r for r in raw[name] if r['request'] in ids]
            allocation = [int(slots[name][r['response_id']]) for r in rows]
            if allocation[0] != 0 or set(allocation[1:]) != {1, 2, 3}:
                raise ValueError('Long/short slot contract')
            details.append(dict(service=name, parent_ttft=rows[0]['ttft'],
                parent_latency=rows[0]['latency'],
                child_start_gap=min(r['start'] for r in rows[1:])-rows[0]['end'],
                child_group_seconds=max(r['end'] for r in rows[1:])-min(r['start'] for r in rows[1:]),
                child_ttft=[r['ttft'] for r in rows[1:]], cache_events=entries,
                parent_session_save_seconds=sum(float(r['ms'])/1000 for r in entries
                    if r['pool']=='session' and r['event']=='save' and r['request']==rows[0]['response_id']),
                seed_save_seconds=sum(float(r['ms'])/1000 for r in entries if r['pool']=='shared')))
        results.append(dict(head='full', scenario=case_id, block=0,
            off_seconds=off['makespan'], on_seconds=on['makespan'],
            time_reduction=1-on['makespan']/off['makespan'],
            complete_outputs_equal=same, cache_contract=cache, details=details))
    if not all(r['complete_outputs_equal'] and r['cache_contract'] for r in results):
        raise ValueError('Completed pair correctness failed')
    if not any(r['time_reduction'] <= 0 for r in results):
        raise ValueError('No frozen stopping condition')
    summary = dict(completed_pair_evidence_valid=True,
        formal_release_passed=False, results=results,
        phase_state='STOPPED', guard_state=read(phase/'lease/exit.json')['status'],
        stop_request=read(BASE/'early-stop-request.json'),
        limits=['Only four full-head block-0 pairs completed; subset and block 1 incomplete.',
                'C has no final sampler barrier; interrupted request is not a performance sample.',
                'Completed services have individual exit, lifecycle and allocation finalization receipts.',
                'Save wall intervals include waiting and IO, not isolated GPU execution or physical NVMe bytes.',
                'Main latency + client gap + child group exactly partitions the observed transaction.',
                'Observed NO_GO threshold does not establish a causal scoring regression or population effect.'])
    (BASE/'stopped-review.json').write_text(json.dumps(summary, indent=2)+'\n')

    # Attribute completed service windows without editing raw files or inventing
    # a shutdown event for the interrupted service. Overall RAM retains all C
    # samples, including its partial tail, and says so explicitly.
    spec = importlib.util.spec_from_file_location('stopped_resources', ROOT/'tools/prefix_seed/resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.existing.summarize

    class CompletedView:
        def __init__(self, path): self.path = path
        def __truediv__(self, key): return self.path/key
        def __str__(self): return str(self.path)
        def glob(self, pattern):
            if pattern != '*/server.log': raise ValueError('Unexpected view query')
            return [self.path/name/'server.log' for name in names]

    def completed(root):
        result = original(CompletedView(root))
        result['limits'].append('Only completed C services have service windows; overall sampled RAM includes interrupted startup/request. No final C monitor barrier.')
        return result
    module.existing.summarize = completed
    module.BASE = BASE
    sys.argv = ['resources', 'c']
    module.main()
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
