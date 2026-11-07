"""Offline audit of the final pair; old numerical evidence, fresh HTTP only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import fcntl

from tools.evalscope import complete_vision_rowmax_mainline as run
from tools.evalscope import audit_vision_rowmax_completion as old

s, read = run.s, run.retained.read
AUDIT = run.BASE/'acceptance-audit.json'
MANIFEST = run.BASE/'originals.json'


def review():
    plan = read(run.FOLDER/'plan.json')
    out = run.FOLDER/'execution'
    result, guard = read(out/'result.json'), read(run.FOLDER/'lease/exit.json')
    s.require(result['status'] == 'CONTRACTS_AND_FULL_MATRIX_COLLECTED_PENDING_REVIEW' and
        not result['failure'] and not result['cleanup_errors'] and not result['monitor_failures'],
        'final matrix did not finish')
    s.require(result['counts']['starts'] == 4 and result['counts']['generations'] == 104 and
        result['counts']['native'] == 0 and 8 <= result['counts']['controls'] <= 64, 'counts')
    s.require(plan['limits'] == run.LIMITS and plan['groups'] == list(run.GROUPS) and
        plan['parent_commit'] == run.PARENT_COMMIT and plan['runtime_diff'] == ['src/vision/vision.cu'],
        'final source/pair scope')
    for key, count in result['counts'].items():
        s.require(len(list(out.glob('attempt-'+key+'-*.json'))) == count <= run.LIMITS[key],
                  'attempt ledger differs')
    sources = old.source_identity(plan)
    for arm, value in plan['binaries'].items():
        s.require(s.sha(value['path']) == value['sha256'], 'final binary drift')
        source_map = read(Path(value['path']).parent/'generated/q4t/server/chat_protocol_sources.json')
        drift = sorted(name for name,digest in source_map.items() if s.sha(s.ROOT/name) != digest)
        s.require(len(source_map) == 107 and drift == ([] if arm == 'candidate' else ['src/vision/vision.cu']),
                  'compiled protocol/runtime drift')
    reused = run.numerical_reuse()
    s.require(reused == plan['reuse'] and not reused['reused_http'], 'numerical reuse drift')
    direct = old.numerical(run.prior.FOLDER/'execution')
    events = run.retained.jsonlines(run.FOLDER/'lease/events.jsonl')
    queries = run.retained.jsonlines(run.FOLDER/'lease/nvml-queries.jsonl')
    s.require(events[0]['event'] == 'lease_acquired' and events[-1]['event'] == 'lease_released'
        and events[-1]['status'] == guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0
        and guard['compute_backend'] == 'nvml' and guard['external_lease'] is False, 'strict lease')
    seconds = events[-1]['monotonic']-events[0]['monotonic']
    s.require(0 < seconds < run.LIMITS['seconds'], 'time budget')
    lifetimes = {g:run.retained.verify_lifetime(out, 'model-'+g, events, queries) for g in run.GROUPS}
    owned = {v['pid'] for v in lifetimes.values()}
    s.require(queries and all(q['status'] == 'OK' and set(q['pids']) <= owned for q in queries),
              'invalid NVML observation')
    s.require(all(not e['conflicts'] and not e['unreadable_processes'] for e in events
                  if e['event'] == 'monitor'), 'environment interference')
    raw, summaries = old.matrix(plan, out, run.GROUPS)
    comparisons = []
    for mode in ('plain','mtp'):
        for case in plan['cases']:
            a,b = (raw[mode+'-'+arm][case['id']] for arm in ('parent','candidate'))
            keys = ('prompt_sha256','prompt_tokens','completion_tokens','text','finish')
            s.require([[r[k] for k in keys] for r in a] == [[r[k] for k in keys] for r in b],
                      'same-mode responses differ')
            if case['id'] != 'quality':
                parent,candidate = (summaries[mode+'-'+arm][case['id']] for arm in ('parent','candidate'))
                comparisons.append(dict(mode=mode, case=case['id'], parent=parent,candidate=candidate,
                    ttft_gain=1-candidate['ttft_mean']/parent['ttft_mean'],
                    decode_gain=candidate['decode_tps']/parent['decode_tps']-1,
                    latency_gain=1-candidate['latency_mean']/parent['latency_mean']))
    for group in run.GROUPS:
        log = (out/('model-'+group)/'stderr.log').read_text()
        s.require(log.count('event=begin stage=validation') == 26, 'server request count')
        caps = read(out/(group+'-caps-wire.bin'))
        s.require(caps['runtime']['mtp_enabled'] == group.startswith('mtp-') and
                  not caps['runtime']['media_enabled'], 'observed runtime mode')
        metrics = sorted(out.glob(group+'-idle-*-wire.bin'))
        s.require(metrics, 'idle observation missing')
        values = s.parse_metrics(metrics[-1].read_text())
        s.require(values['q4t_requests_total'] == values['q4t_requests_success_total'] == 26 and
                  values['q4t_requests_error_total'] == values['q4t_requests_aborted_total'] == 0,
                  'actual server outcomes')
    final = read(out/'gpu-final.json')
    s.require(final['status'] == 'OK' and final['pids'] == [] and queries[-1]['pids'] == [],
              'final GPU observation')
    pids = owned | {q['worker_pid'] for q in queries}
    for e in events:
        pids.add(e['pid'])
        if 'child_pid' in e: pids.add(e['child_pid'])
    for p in out.rglob('client-process.json'):
        row = read(p)
        if 'pid' in row: pids.add(row['pid'])
    s.require(all(not Path(f'/proc/{pid}').exists() for pid in pids), 'owned PID remains')
    with (s.ROOT/'.q4t-work/gpu-exclusive.lock').open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = os.fstat(lock.fileno())
        s.require((st.st_dev,st.st_ino) == (plan['lock_device'],plan['lock_inode']), 'lock replaced')
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',plan['port']))
            sock.listen(1)
    pilot = read(run.retained.BASE/'audit.json')
    targets = [row for row in pilot['pairs'] if row['target_case']]
    s.require(len(targets) == 4 and all(row['latency_reduction'] >= .03 for row in targets),
              'retained pilot investment gate')
    audit = dict(status='PASS_FINAL_MAINLINE_MATRIX_PENDING_ENGINEERING_DECISION',
        source_commit=plan['source_commit'], binaries=plan['binaries'], git_sources=sources,
        counts=result['counts'], lease_seconds=seconds, nvml_queries=len(queries),
        new_lifetimes=lifetimes, numerical_reuse=reused, numerical=direct,
        rows=raw,summaries=summaries,comparisons=comparisons,pilot_pairs=pilot['pairs'],
        quality_checks=44,paired_responses=52,raw_http_reused=False,
        statistics='inconclusive; one start per identity/mode; no statistical noninferiority',
        performance_accepted=False,default_unchanged=True,
        cleanup=dict(owned_pids_reaped=len(pids),gpu_empty=True,port_free=True,lock_available=True),
        audit_tool=dict(path=str(Path(__file__).resolve()),sha256=s.sha(__file__)))
    s.save(AUDIT,audit)
    return {k:v for k,v in audit.items() if k in ('status','counts','lease_seconds','comparisons','cleanup')}


def seal():
    plan = read(run.FOLDER/'plan.json')
    paths = {Path(p) for p in plan['files']}
    paths.update(p for p in run.FOLDER.rglob('*') if p.is_file())
    paths.update((AUDIT,Path(__file__).resolve()))
    cas = run.BASE/'originals'
    cas.mkdir(mode=0o700)
    files = {}
    for path in sorted(paths):
        digest = s.sha(path)
        if str(path) in plan['files']:
            s.require(digest == plan['files'][str(path)], 'input changed before seal')
        target = cas/digest
        if not target.exists(): shutil.copyfile(path,target)
        s.require(s.sha(target) == digest,'sealed copy differs')
        files[str(path)] = digest
    s.save(MANIFEST,dict(files=files))
    return dict(files=len(files),objects=len(set(files.values())),manifest_sha256=s.sha(MANIFEST))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('review','seal'))
    action = parser.parse_args().action
    print(json.dumps({'review':review,'seal':seal}[action](),ensure_ascii=False))
