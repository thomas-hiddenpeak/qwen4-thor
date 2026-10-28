"""Audit finite request/start/fixture ledger and retained failures on CPU."""
import hashlib
import json
from pathlib import Path
import re
import time

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-cache-20261028'
def read(path):return json.loads(path.read_text())
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    phases=[];requests=starts=fixtures=0;failures=[]
    identity=read(BASE/'identity.json')
    for name in ['phase-a','phase-b','phase-b-reference','phase-c','phase-d']:
        directory=BASE/name
        if not (directory/'run/state.json').exists():continue
        plan=read(directory/'plan.json');state=read(directory/'run/state.json')
        if state['status']=='RUNNING':raise ValueError('cannot finalize live stage')
        post=read(directory/'postflight.json');guard=read(directory/'lease/exit.json')
        summary=read(directory/'run/telemetry-summary.json')
        if (state.get('monitor_failures') or post.get('monitor_failures') or post['status']=='ENV_ABORT' or
            post['final_monitor_errors'] or post['cleanup_errors'] or
            summary['sample_attempts']!=summary['flushed_samples'] or summary['unflushed_attempts'] or
            summary.get('sampler_failure') or summary.get('failures_at_exit') or
            summary.get('exit_pending',{}).get('active')):
            failures.append(name+': environment')
        old_reference=name=='phase-b'
        if not old_reference and (state['status']!='MEASURED_REQUIRES_REVIEW' or guard['status']!='COMPLETED'):
            failures.append(name+': incomplete')
        if not old_reference and not post['final_monitor_barrier']:
            failures.append(name+': missing final monitor barrier')
        if (post['state_sha256']!=sha(directory/'run/state.json') or
                post['final_summary_sha256']!=sha(directory/'run/telemetry-summary.json') or
                state['plan_sha256']!=sha(directory/'plan.json')):
            failures.append(name+': seal identity')
        if state['finished']>identity['deadline']:
            failures.append(name+': deadline exceeded')
        if old_reference and (state['status']!='STOPPED' or 'complete state differs' not in
                (directory/'run/00-full-on-driver.log').read_text()):
            failures.append(name+': original failure changed')
        bindings={}
        for path,metadata in plan['model_file_metadata'].items():
            st=Path(path).stat()
            if metadata!={'size':st.st_size,'mtime_ns':st.st_mtime_ns,
                          'device':st.st_dev,'inode':st.st_ino}:
                failures.append(name+': model file identity '+path)
        for path,digest in plan['files'].items():
            source=Path(path)
            if sha(source)!=digest:
                original=BASE/'phase-b-reference/original-state-contract.cpp'
                if path==str(ROOT/'tools/prefix_cache/state_contract.cpp') and sha(original)==digest:source=original
                else:failures.append(name+': unresolved binding '+path);continue
            bindings[path]=str(source)
        actual=0;services=0
        for log in (directory/'run').glob('*/server.log'):
            raw=log.read_text();services+=1
            actual+=len(re.findall(r'event=admitted ',raw))
            for line in raw.splitlines():
                if line.startswith('[prefix-cache] request=') and 'error=none' not in line and 'error=session cache full' not in line:
                    failures.append(name+': shared save error '+line)
        direct=sum(s['kind']=='contract' and (directory/'run'/(s['name']+'-driver.log')).exists() for s in plan['steps'])
        requests+=actual;starts+=services;fixtures+=direct
        phases.append(dict(name=name,status=state['status'],actual_requests=actual,actual_starts=services,
            actual_gpu_fixtures=direct,elapsed_seconds=state['elapsed_seconds'],
            source_bindings=bindings,plan_sha256=sha(directory/'plan.json'),
            sampler_flushed=summary['flushed_samples'],final_barrier=bool(post['final_monitor_barrier']),
            retained_limitation='Original failed fixture stage lacks final post-shutdown barrier; all sampled observations flushed and no monitor error.' if old_reference else None))
    if requests>200 or starts>14 or fixtures>4:failures.append('total resource budget')
    result=dict(passed=not failures,failures=failures,phases=phases,cumulative_requests=requests,
        cumulative_starts=starts,cumulative_gpu_fixtures=fixtures,deadline=identity['deadline'],
        deadline_remaining_seconds=identity['deadline']-time.time(),
        note_wall_clock='Phase finish timestamps checked against original absolute deadline; archive/review time reported separately.',
        statistical_noninferiority='NOT_ESTABLISHED',release_acceptance=False,
        note='Evidence validity only; original failure retained and inference/performance gates reviewed separately.')
    (BASE/'final-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='phases'},indent=2))
    if failures:raise SystemExit(1)


if __name__=='__main__':main()
