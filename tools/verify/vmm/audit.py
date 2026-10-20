"""Audit bounded VMM evidence; ENV_ABORT is never runtime acceptance."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT/'.q4t-work/vmm-prototype-20261020'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    evidence = {}
    for name in ('capability-v1', 'replay-v1'):
        stage = BASE/name
        manifest = read(stage/'sealed-files.json')
        for rel, expected in manifest.items():
            assert sha(stage/rel)==expected, rel
        queries = [json.loads(line) for line in
                   (stage/'lease/nvml-queries.jsonl').read_text().splitlines()]
        evidence[name] = dict(seal=read(stage/'seal.json'), files=len(manifest),
            query_statuses=[row['status'] for row in queries])
    capability=(BASE/'capability-v1/driver.log').read_text()
    assert 'vmm_supported=1' in capability
    assert 'minimum=2097152 recommended=2097152' in capability
    assert capability.count('PASS')==3
    assert evidence['capability-v1']['seal']['guard_status']=='COMPLETED'
    run=BASE/'replay-v1/run'
    assert sha(BASE/'replay')==sha(run/'replay')
    for rel, expected in read(run/'sources.json').items():
        assert sha(run/'source'/rel)==expected, rel
    source=read(run/'replay-plan.json')
    assert sha(ROOT/source['source'])==source['source_sha256']
    expected_rows=re.findall(r'\[alloc-observer\] (\w+) begin id=\d+ bytes=(\d+)',
                            (ROOT/source['source']).read_text())
    expected_plan=''.join(f'{api} {size}\n' for api,size in expected_rows)
    assert (run/'allocations.txt').read_text()==expected_plan
    assert len(expected_rows)==1493
    log=(run/'replay.log').read_text()
    assert 'owner contracts PASS' in log and 'replay PASS' not in log
    begins=re.findall(r'begin id=(\d+) api=(\w+) bytes=(\d+) mono=([\d.]+)',log)
    ends=re.findall(r'end id=(\d+) api=(\w+) bytes=(\d+) mono=([\d.]+)',log)
    assert len(begins)==1493 and len(ends)==1492
    assert [(a,b) for _,a,b,_ in begins]==expected_rows
    assert [int(i) for i,_,_,_ in ends]==list(range(1492))
    assert begins[-1][2]=='2348023808'
    cache=read(run/'cache-after-prewarm.json')
    assert cache['complete_file_set_observed']
    cache_bytes=sum(f['resident_bytes'] for f in cache['files'])
    assert cache_bytes>=48<<30
    witnesses=[]
    for line in (run/'stacks.jsonl').read_text().splitlines():
        row=json.loads(line)
        if row['monotonic']<float(begins[-1][3]):continue
        kinds=set()
        for target in row['targets']:
            stack='\n'.join(t['stack']['value'] or '' for t in target['tasks'])
            if target['kind']=='model' and 'compact' in stack:
                kinds.add('model')
            if target['kind']=='nvml' and 'rmapiLockAcquire' in stack:
                kinds.add('nvml')
        if len(kinds)==2:witnesses.append(row['monotonic'])
    assert witnesses
    assert evidence['replay-v1']['seal']['guard_status']=='ENV_ABORT'
    assert 'TIMEOUT' in evidence['replay-v1']['query_statuses']
    assert read(run/'isolation/cleanup.json')['unit_removed']
    default=sha(ROOT/'build/q4t')
    assert default=='19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
    result=dict(status='VERIFIED_DIAGNOSTIC_FAILURE', stages=evidence,
        cache_bytes_before_replay=cache_bytes, begun=len(begins), completed=len(ends),
        unfinished=begins[-1], simultaneous_wait_samples=witnesses,
        probe_sha256=sha(BASE/'probe'), replay_sha256=sha(run/'replay'),
        default_sha256=default, model_loads=0, http_requests=0,
        production_accepted=False, performance_acceptance=False,
        full_replay_release_tested=False,
        decision='STOP_PROTOTYPE_WITHOUT_MODEL_INTEGRATION; ENV_ABORT not performance NO_GO')
    (BASE/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('status','cache_bytes_before_replay','begun','completed')}))


if __name__=='__main__':main()
