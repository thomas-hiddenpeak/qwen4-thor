"""Continue the remaining N04 handoff business without replaying passed work."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_access import acceptance as a
from tools.serve_access.resume import sha

BASE = ROOT/'.q4t-work/serve-access-20261109'


def main():
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):
        raise ValueError('commit reviewed checkpoint driver first')
    output = BASE/'business-04'
    if output.exists():raise ValueError('checkpoint continuation exists')
    prior = BASE/'business-03';execution = prior/'execution';state = execution/'state'
    old = json.loads((prior/'plan.json').read_bytes())
    result = json.loads((execution/'result.json').read_bytes())
    first = json.loads((execution/'first-stop.json').read_bytes())
    ready = json.loads((execution/'ready-1.json').read_bytes())
    sealed = json.loads((execution/'first-sealed.json').read_bytes())
    cpu = json.loads((BASE/'checkpoint-cpu-01/result.json').read_bytes())
    if cpu['exit_code'] != 0 or cpu['count'] != 4:raise ValueError('checkpoint CPU failed')
    if not (first['cleanup_confirmed'] and first['stop_durable'] and first['state'] == 'STOPPED' and
            sealed['phase'] == 'sealed' and sealed['generation'] == 1 and
            result['budget']['counts']['starts'] == 2 and result['budget']['counts']['chats'] == 10):
        raise ValueError('not the sealed pre-second-start checkpoint')
    plan = copy.deepcopy(old)
    plan.update(primary_package=old['package'],package=old['second_package'])
    a.load_modules(plan)
    from tools.serve_recovery import access_policy,access_store,deployment,journal,store,usage_store
    from tools.serve_service import package as engine
    owned = [dict(pid=ready[k],start_ticks=ready[k.replace('_pid','_start_ticks')])
             for k in ('guardian_pid','controller_pid','model_pid','gateway_pid','journal_pid')]
    for item in owned:
        try:
            if journal.process_start(item['pid']) == item['start_ticks']:
                raise ValueError('old owned process still exists')
        except (FileNotFoundError,ProcessLookupError):pass
    ports = engine.read(Path(old['package']['package'])/'gateway-policy.json')
    for port in (ports['listen_port'],ports['upstream_port'],ports['upstream_operations_port']):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    key_path = execution/'revoke-during-limits-loss-private.json'
    current_keys = engine.read(key_path)
    with deployment.claim(state), deployment.claim(state,name='guardian.lock'):
        desc = deployment.read_descriptor(state)
        engine.require(desc['package'] == old['second_package']['package'] and
            desc['manifest_sha256'] == old['second_package']['manifest_sha256'], 'checkpoint selection changed')
        deployment.ready(state,desc)
        with store.Store(state,desc['service_id']) as core:
            engine.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                           'checkpoint core is active')
            binding = access_store.binding(state,desc['service_id'])
            with usage_store.open_bound(binding) as usage:
                engine.require(usage.head['state']['policy'] == sealed,'sealed handoff changed')
            with access_store.open_bound(binding) as keys:
                active = keys.head['state']['policy']['clients']
                expected = [dict(id=row['id'],key_sha256=[access_policy.key_digest(k) for k in row['api_keys']])
                            for row in current_keys['clients']]
                engine.require(active == expected,'checkpoint credentials changed')
            protected = [p for p in state.rglob('*.json') if 'runs' not in p.relative_to(state).parts]
            inputs = [dict(path=str(p),sha256=sha(p)) for p in sorted(protected)+[key_path]]
    output.mkdir(mode=0o700)
    check = dict(status='PASS_STOPPED_DEPLOYMENT_CHECKPOINT',models_started_after_deploy=0,
        owned_processes_gone=True,ports_released=True,locks_released=True,owned_identities=owned,
        service_id=desc['service_id'],descriptor=desc,model_starts=0,chat_requests=0,
        original_cleanup_failure_preserved=True,checkpoint_not_reseeded=True)
    a.save(output/'checkpoint-cleanup.json',check)
    seconds = sum(engine.read(BASE/f'business-launch-{i:02d}/result.json')['seconds'] for i in (1,2,3))
    plan.update(output=str(output),state_directory=str(state),checkpoint='sealed_deployment',
        checkpoint_descriptor=desc,checkpoint_inputs=inputs,checkpoint_keys=str(key_path),
        checkpoint_sealed=str(execution/'first-sealed.json'),checkpoint_stored=str(execution/'beta-stored-response.json'),
        checkpoint_from=str(execution),checkpoint_cleanup=str(output/'checkpoint-cleanup.json'),
        prior_execution=str(execution/'result.json'),prior_guard=str(prior/'lease/exit.json'),
        prior_counts=result['budget']['counts'],prior_seconds=seconds,prior_failure_category='sealed_deployment',
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    allowed = {'tools/serve_access/acceptance.py','tools/serve_access/test_acceptance.py',
               'tools/serve_recovery/acceptance.py'}
    changes = []
    for name,digest in old['sources'].items():
        current = sha(ROOT/name)
        if current != digest:
            if name not in allowed:raise ValueError('runtime input changed: '+name)
            changes.append(dict(path=name,before=digest,after=current))
        plan['sources'][name] = current
    plan['sources']['tools/serve_access/checkpoint.py'] = sha(__file__)
    plan['source_changes'] = changes
    paths = [prior/'plan.json',*sorted((prior/'lease').iterdir()),
        *sorted((BASE/'business-launch-03').iterdir()),output/'checkpoint-cleanup.json',
        BASE/'checkpoint-cpu-01/result.json']
    paths += [p for p in execution.iterdir() if p.is_file() and
              (p.name.endswith(('-response.json','-management.json')) or p.name in
               ('result.json','counts.json','first-stop.json','first-sealed.json','second-launcher.log'))]
    plan['prior_evidence'] += [dict(path=str(p),sha256=sha(p)) for p in paths if p.is_file()]
    a.verify(plan)
    engine.require(seconds < plan['limits']['seconds']-180,'insufficient remaining time')
    a.save(output/'plan.json',plan)
    print(json.dumps(dict(plan=str(output/'plan.json'),prior_counts=plan['prior_counts'],
        remaining_seconds=plan['limits']['seconds']-seconds,resume_generation=2,passed_work_replayed=False)))


if __name__ == '__main__':main()
