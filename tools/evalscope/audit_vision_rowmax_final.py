"""Accept a complete comparison only from four uninterrupted lifetimes."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket

from tools.evalscope import finish_vision_rowmax_parent as r

s,read=r.s,r.read
BASE=r.BASE
AUDIT=BASE/'acceptance-audit.json'


def review():
    reused=r.prefix()
    plan=read(r.FOLDER/'plan.json')
    original=read(r.original.FOLDER/'plan.json')
    s.require(reused == plan['completed_prefix'],'completed prefix drift')
    out=r.FOLDER/'execution'
    result,guard=read(out/'result.json'),read(r.FOLDER/'lease/exit.json')
    counts=result['counts']
    s.require(result['status']=='CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW'
        and not result['failure'] and not result['cleanup_errors']
        and not result['monitor_failures'],'completion failed')
    s.require(plan['limits']==r.LIMITS and plan['groups']==list(r.GROUPS)
        and counts['starts']==1 and counts['generations']==26
        and counts['native']==0 and 2<=counts['controls']<=16,'completion budget')
    for key,count in counts.items():
        s.require(len(list(out.glob('attempt-'+key+'-*.json')))==count,'attempt ledger')
    sources=r.audit.source_identity(plan)
    for arm,value in plan['binaries'].items():
        s.require(value==original['binaries'][arm] and s.sha(value['path'])==value['sha256'],
                  'tested pair drift')
        source=read(Path(value['path']).parent/'generated/q4t/server/chat_protocol_sources.json')
        drift=sorted(n for n,h in source.items() if s.sha(s.ROOT/n)!=h)
        s.require(len(source)==107 and drift==([] if arm=='candidate' else ['src/vision/vision.cu']),
                  'compiled runtime differs')
    numerical=r.original.numerical_reuse()
    s.require(numerical==plan['reuse'] and not numerical['reused_http'],'numerical reuse drift')
    events=r.original.retained.jsonlines(r.FOLDER/'lease/events.jsonl')
    queries=r.original.retained.jsonlines(r.FOLDER/'lease/nvml-queries.jsonl')
    s.require(guard['status']==events[-1]['status']=='COMPLETED'
        and guard['child_returncode']==0 and guard['compute_backend']=='nvml'
        and guard['external_lease'] is False and events[0]['event']=='lease_acquired'
        and events[-1]['event']=='lease_released','strict completion lease')
    elapsed=events[-1]['monotonic']-events[0]['monotonic']
    s.require(0<elapsed<r.LIMITS['seconds'],'completion deadline')
    lifetime=r.original.retained.verify_lifetime(out,'model-mtp-parent',events,queries)
    s.require(queries and all(q['status']=='OK' and set(q['pids'])<={lifetime['pid']} for q in queries),
              'completion NVML gap or competitor')
    s.require(all(not e['conflicts'] and not e['unreadable_processes'] for e in events
        if e['event']=='monitor'),'completion interference')
    raw,summaries=r.audit.matrix(original,r.original.FOLDER/'execution',r.REUSED)
    fresh,summary=r.audit.matrix(plan,out,r.GROUPS)
    raw.update(fresh);summaries.update(summary)
    comparisons=[]
    for mode in ('plain','mtp'):
        for case in plan['cases']:
            a,b=(raw[mode+'-'+arm][case['id']] for arm in ('parent','candidate'))
            keys=('prompt_sha256','prompt_tokens','completion_tokens','text','finish')
            s.require([[x[k] for k in keys] for x in a]==[[x[k] for k in keys] for x in b],
                      'same-mode responses differ')
            if case['id']!='quality':
                parent,candidate=(summaries[mode+'-'+arm][case['id']] for arm in ('parent','candidate'))
                comparisons.append(dict(mode=mode,case=case['id'],parent=parent,candidate=candidate,
                    ttft_gain=1-candidate['ttft_mean']/parent['ttft_mean'],
                    decode_gain=candidate['decode_tps']/parent['decode_tps']-1,
                    latency_gain=1-candidate['latency_mean']/parent['latency_mean']))
    for group in r.original.GROUPS:
        directory=out if group=='mtp-parent' else r.original.FOLDER/'execution'
        log=(directory/('model-'+group)/'stderr.log').read_text()
        s.require(log.count('event=begin stage=validation')==26,'actual request count')
        caps=read(directory/(group+'-caps-wire.bin'))
        s.require(caps['runtime']['mtp_enabled']==group.startswith('mtp-')
            and not caps['runtime']['media_enabled'],'actual runtime mode')
        values=s.parse_metrics(sorted(directory.glob(group+'-idle-*-wire.bin'))[-1].read_text())
        s.require(values['q4t_requests_total']==values['q4t_requests_success_total']==26
            and values['q4t_requests_error_total']==values['q4t_requests_aborted_total']==0,
            'actual server outcomes')
    final=read(out/'gpu-final.json')
    s.require(final['status']=='OK' and final['pids']==queries[-1]['pids']==[],'final GPU')
    pids=set()
    for folder in (r.original.FOLDER,r.FOLDER):
        for q in r.original.retained.jsonlines(folder/'lease/nvml-queries.jsonl'):
            pids.add(q['worker_pid'])
        for p in (folder/'execution').rglob('identity.json'):
            pids.add(read(p)['pid'])
        for p in (folder/'execution').rglob('client-process.json'):
            if 'pid' in read(p):pids.add(read(p)['pid'])
        for e in r.original.retained.jsonlines(folder/'lease/events.jsonl'):
            pids.add(e['pid'])
            if 'child_pid' in e:pids.add(e['child_pid'])
    s.require(all(not Path(f'/proc/{pid}').exists() for pid in pids),'owned PID remains')
    with (s.ROOT/'.q4t-work/gpu-exclusive.lock').open() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        st=os.fstat(lock.fileno())
        s.require((st.st_dev,st.st_ino)==(plan['lock_device'],plan['lock_inode']),'lock replaced')
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',plan['port']));sock.listen(1)
    pilot=read(r.original.retained.BASE/'audit.json')
    targets=[x for x in pilot['pairs'] if x['target_case']]
    s.require(len(targets)==4 and all(x['latency_reduction']>=.03 for x in targets),'pilot target')
    value=dict(status='PASS_FINAL_MAINLINE_MATRIX_PENDING_ENGINEERING_DECISION',
        source_commit=original['source_commit'],completion_tool_commit=plan['source_commit'],
        binaries=plan['binaries'],git_sources=sources,completed_prefix=reused,
        completion_lifetime=lifetime,completion_counts=counts,completion_seconds=elapsed,
        completion_nvml_queries=len(queries),accepted_requests=104,quality_checks=44,
        paired_responses=52,total_attempts=dict(starts=5,generations=130),
        original_stage_status='ENV_ABORT',original_failed_lifetime_reused=False,
        numerical_reuse=numerical,numerical=r.audit.numerical(r.original.prior.FOLDER/'execution'),
        rows=raw,summaries=summaries,comparisons=comparisons,pilot_pairs=pilot['pairs'],
        statistics='inconclusive; one complete start per identity/mode; no statistical noninferiority',
        performance_accepted=False,default_unchanged=True,
        cleanup=dict(owned_pids_reaped=len(pids),gpu_empty=True,port_free=True,lock_available=True),
        audit_tool=dict(path=str(Path(__file__).resolve()),sha256=s.sha(__file__)))
    s.save(AUDIT,value)
    return {k:value[k] for k in ('status','completion_counts','completion_seconds',
        'completion_nvml_queries','comparisons','cleanup')}


def seal():
    paths={AUDIT,Path(__file__).resolve(),BASE/'acceptance-abort.json',BASE/'aborted-originals.json'}
    for folder in (r.original.FOLDER,r.FOLDER):
        paths.update(Path(p) for p in read(folder/'plan.json')['files'])
        paths.update(p for p in folder.rglob('*') if p.is_file())
    cas=BASE/'originals';cas.mkdir(mode=0o700)
    files={}
    for path in sorted(paths):
        digest=s.sha(path);dest=cas/digest
        if not dest.exists():shutil.copyfile(path,dest)
        s.require(s.sha(dest)==digest,'sealed copy differs');files[str(path)]=digest
    s.save(BASE/'originals.json',dict(files=files))
    return dict(files=len(files),manifest_sha256=s.sha(BASE/'originals.json'))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('review','seal'))
    action=parser.parse_args().action
    print(json.dumps({'review':review,'seal':seal}[action](),ensure_ascii=False))
