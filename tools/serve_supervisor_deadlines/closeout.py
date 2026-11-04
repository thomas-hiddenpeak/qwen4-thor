"""Reconcile the preserved failed run and one targeted production stop recheck."""
import argparse
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_supervisor_deadlines import finish as f,report as original
from tools.serve_operations_managed import report as archive_base

s=f.s
pkg=s.pkg
ARCHIVE=ROOT/'docs/evidence/serve-supervisor-deadlines-20261104'


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def healthy_guard(directory,external=False):
    terminal=pkg.read(directory/'terminal.json')
    pkg.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
        terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and
        terminal['worker']['replayed_requests']==0 and terminal['guard']['status']=='COMPLETED' and
        terminal['guard']['external_lease']==external,'production controller terminal')
    events=read_events(directory/'lease/events.jsonl')
    polls=[r for r in events if r['event']=='monitor']
    pkg.require(events[0]['event']=='lease_acquired' and
        events[-1]['event']==('lease_guard_finished' if external else 'lease_released') and
        polls and all(not r['conflicts'] and r['unreadable_processes']==0 for r in polls),'production guard observations')
    return events,len(polls)


def audit():
    recheck=f.check();plan=s.check();root=s.OUT
    result=pkg.read(root/'result.json')
    pkg.require(result['status']=='ENV_ABORT' and result['counts']==
        dict(starts=2,generation_attempts=4,controls=16),'initial failure must remain')
    initial_cpu=pkg.read(s.BASE/'baseline/provenance.json')
    pkg.require(initial_cpu['observed_failures']==3 and initial_cpu['observed_errors']==0 and
        pkg.read(s.BASE/'baseline/result.json')['returncode']==1 and
        'FAILED (failures=3)' in (s.BASE/'baseline/unittest.stderr').read_text(),'original defects')
    first=pkg.read(root/'first-ready.json')
    pressure=pkg.read(root/'saturated-supervisor.json')
    pkg.require(s.base.expected_saturation(pressure,first['updated_unix']) and
        pressure['backend']==first['backend'],'fresh same-backend pressure')
    diagnostic=pkg.read(root/'saturated-diagnostic.json')
    pkg.require(diagnostic['state']=='HEALTHY' and diagnostic['identity']['state']=='verified',
                'pressure diagnostic')
    pkg.require(pkg.same_json(json.loads((root/'business/saturated-capabilities-wire.bin').read_bytes()),
        pkg.read(Path(plan['package']['package'])/'declarations/capabilities.json')),'live declaration')
    responses=[]
    for client,ceiling in (('alpha',512),('beta',64)):
        for part in ('read','answer'):
            name=f'project-{client}-{part}'
            stored=pkg.read(root/'business'/(name+'-response.json'));seconds=stored.pop('elapsed')
            parsed=s.base.client.a.read_sse(io.BytesIO((root/'business'/(name+'-wire.bin')).read_bytes()))
            request=pkg.read(root/'business'/(name+'-request.json'))['body']
            pkg.require(asdict(parsed)==stored and parsed.id==request['request_id'] and
                parsed.model==request['model'] and request['max_tokens']<=ceiling and
                parsed.usage['completion_tokens']<=ceiling,'wire/request/client budget')
            if part=='answer':
                tool=json.loads(pkg.read(root/'business'/f'project-{client}-tool-result.json')['content'])
                source=tool['source']
                pkg.require(tool['ok'] and source['path']=='docs/MODEL.md' and
                    source['start_line']==1 and source['end_line']==24 and
                    source['sha256']==pkg.sha(ROOT/'docs/MODEL.md'),'real tool source')
                s.base.client.a.validate_answer(parsed.message['content'],{source['citation']},1)
                pkg.require('Qwen4ExpForConditionalGeneration' in parsed.message['content'],'answer value')
            responses.append(dict(client=client,part=part,seconds=seconds,usage=parsed.usage))
    owners=[dict(role='controller',**pkg.read(root/'controller-identity.json')),
        dict(role='worker',**first['owner']),dict(role='backend',**first['backend'])]
    for index in (1,2):
        front=pkg.read(root/f'front-{index}-ready.json')
        terminal=pkg.read(root/f'front-{index}/terminal.json')
        pkg.require(front['control']['binding']['backend']==first['backend'] and
            front['control']['binding']['operations_port']==s.base.OPS_PORT and
            terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            not terminal['automatic_reactivation'],'gateway lifecycle')
        owners += [dict(role=f'front-{index}',**front['control']['owner']),
                   dict(role=f'nginx-{index}',**front['state']['observation']['nginx'])]
    for index in (0,1):
        terminal=pkg.read(root/f'rejected-{index}/terminal.json')
        pkg.require(terminal['state']=='FAILED' and terminal['cleanup_confirmed'] and
            'backend operations port differs' in terminal['error'] and
            not (root/f'rejected-{index}/status.json').exists(),'gateway rejected before child')
    metrics=pkg.read(root/'final-metrics.json')
    for name in ('q4t_active_chats','q4t_public_connections','q4t_business_connections',
        'q4t_request_body_bytes','q4t_structured_workers_active','q4t_structured_active',
        'q4t_structured_preparing'):
        pkg.require(metrics[name]==0,'resource retained: '+name)
    pkg.require(metrics['q4t_requests_success_total']==4 and
        metrics['q4t_requests_error_total']==metrics['q4t_requests_aborted_total']==0 and
        metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4 and
        metrics['q4t_seq_slots_free']==1,'request/worker accounting')
    first_guard,first_polls=healthy_guard(root/'managed-run')
    interrupted_guard,interrupted_polls=healthy_guard(root/'startup-run',True)
    custody=read_events(root/'custody/events.jsonl')
    valid=custody[:-1]
    pkg.require(valid and custody[-1]['status']=='ENV_ABORT' and
        all(not r['observation']['conflicts'] and r['observation']['unreadable_processes']==0 for r in valid) and
        valid[0]['monotonic']<first_guard[-1]['monotonic']<valid[-1]['monotonic']<custody[-1]['monotonic'] and
        pkg.read(root/'custody/exit.json')['status']=='ENV_ABORT' and
        pkg.read(root/'custody/exit.json')['cleanup_confirmed'],'initial failure boundary')
    transport=pkg.read(root/'transport/result.json')
    pkg.require(transport['passed'] and transport['requests']==len(transport['rows'])==3 and
        transport['transport_sha256']==pkg.sha(ROOT/'tools/serve_ops/transport.py'),'sealed transport')
    for row,expected in zip(transport['rows'],('observation_stopped','http_deadline',None)):
        pkg.require(row['reason']==expected and row['seconds']<.7 and row['peer_closed'],'transport result')
    if transport['rows'][-1]['response'] is not None:
        pkg.require(transport['rows'][-1]['response']['http_status']==200,'recovered transport')
    old_attempt=pkg.read(root/'startup-run/attempt-1.json')
    old_state=pkg.read(root/'startup-run/status.json')
    owners += [dict(role='initial-startup-controller',**pkg.read(root/'startup-controller-identity.json')),
        dict(role='initial-startup-worker',**old_state['owner']),
        dict(role='initial-startup-backend',**old_attempt['owner']),
        dict(role='transport-probes',**pkg.read(root/'transport-probes-identity.json'))]
    output=f.OUT;retry=pkg.read(output/'result.json')
    pkg.require(retry['status']=='PASS_STARTUP_STOP_RECHECK' and retry['cleanup_confirmed'] and
        retry['attempts']==1 and retry['complete_models']==retry['generation_attempts']==retry['real_tools']==0 and
        retry['seconds']<recheck['seconds'],'targeted outcome')
    before=pkg.read(output/'before-stop.json');stopped=pkg.read(output/'stop-result.json')
    pkg.require(s.startup_snapshot(before['state'],'',before['peers']) and stopped['passed'] and
        stopped['no_ready_publication'] and stopped['seconds']<recheck['stop_seconds'] and
        stopped['terminal_sha256']==pkg.sha(output/'managed-run/terminal.json') and
        '[q4t] serving on port' not in (output/'managed-run/backend.log').read_text() and
        not any(r.endswith((' READY',' DEGRADED')) for r in
            (output/'managed-run/events.log').read_text().splitlines()),'loading stop evidence')
    retry_guard,retry_polls=healthy_guard(output/'managed-run')
    retry_custody=read_events(output/'custody/events.jsonl')
    retry_copy=pkg.read(output/'custody/lease-copy.json')
    initial_copy=pkg.read(root/'custody/lease-copy.json')
    pgid=pkg.read(output/'monitor-root.json')
    pkg.require(pgid['owner']['pid']==pgid['actual_pgid']==pgid['monitor_pgid'] and
        retry_copy['owner']==pgid['owner']==pkg.read(output/'controller-identity.json') and
        retry_copy['device']==initial_copy['device']==66305 and
        retry_copy['inode']==initial_copy['inode']==3294067 and
        all(not r['observation']['conflicts'] and r['observation']['unreadable_processes']==0 for r in retry_custody) and
        pkg.read(output/'custody/exit.json')['status']=='COMPLETED' and
        retry_guard[0]['monotonic']<=retry_custody[0]['monotonic']<retry_guard[-1]['monotonic']<=retry_custody[-1]['monotonic'],
        'single production-root custody')
    final=pkg.read(output/'stopped-inspection.json')
    pkg.require(final['complete'] and final['cleanup_observed'] and not final['listeners'] and
        not final['gpu']['pids'] and final['lock']['state']=='AVAILABLE','targeted final inspection')
    owners += [dict(role='recheck-controller',**pgid['owner']),
        dict(role='recheck-worker',**before['state']['owner']),
        dict(role='recheck-backend',**before['state']['backend'])]
    for owner in owners:
        pkg.require(not s.base.gateway.alive({k:owner[k] for k in ('pid','start_ticks','boot_id')}),'owned identity remains')
    pkg.require(pkg.read(s.BASE/'postflight/result.json')['passed'] and
        pkg.read(s.BASE/'recheck-postflight/result.json')['passed'],'postflight resources')
    initial_seconds=custody[-1]['monotonic']-first_guard[0]['monotonic']
    retry_seconds=retry_custody[-1]['monotonic']-retry_guard[0]['monotonic']
    pkg.require(initial_seconds<s.GPU_SECONDS and retry_seconds<recheck['gpu_timer_seconds'],'lease budget')
    return dict(status='ACCEPT_SUPERVISOR_DEADLINES_SCOPED',package=plan['package'],
        previous_package=plan['previous_package'],source_commit=plan['source_commit'],
        recheck_commit=recheck['source_commit'],default_sha256=pkg.sha(ROOT/'build/q4t'),
        sidecar_sha256=pkg.sha(ROOT/'build/q4t.release.json'),protocol=plan['protocol'],
        cpu_checks=114,driver_checks=6,recheck_cpu_checks=3,original_cpu_failures=3,
        original_run_status='ENV_ABORT',original_custody_status='ENV_ABORT',
        original_failure_explanation='driver PID used as PGID; reproduced in isolation, original conflict list absent',
        successful_prefix_reused=True,continuous_successful_whole_run=False,
        actual_backend_attempts=3,complete_models=1,successful_responses=4,real_tools=2,
        startup_stops_accepted=1,startup_stop_seconds=stopped['seconds'],responses=responses,
        transport=transport,gateway_starts=2,rejected_gateways=2,
        public_connections_at_new_supervisor_sample=136,
        guard_observations=[first_polls,interrupted_polls,retry_polls],
        custody_observations=[len(valid),len(retry_custody)],
        initial_guard_seconds=first_guard[-1]['monotonic']-first_guard[0]['monotonic'],
        initial_lease_until_abort_seconds=initial_seconds,recheck_lease_seconds=retry_seconds,
        monitored_shared_lock_seconds=initial_seconds+retry_seconds,
        lease_time_scope='sum of two separate intervals; initial includes preserved ENV_ABORT',
        initial_driver_seconds=result['seconds'],recheck_driver_seconds=retry['seconds'],
        owned_processes=owners,cleanup_observed=True,release_declarations_verified_live=True,
        default_changed=False,host_envelope_enabled_in_package=False,
        physical_ram_hard_limit_proved=False,actual_lan_accepted=False,
        long_runs_executed=False,persistent_installation=False,performance_acceptance=False)


def configure():
    original.configure()
    archive_base.audit=audit
    archive_base.ARCHIVE=ARCHIVE


def archive():
    result=audit()
    pkg.save(s.BASE/'accepted-package.json',dict(package=result['package'],
        acceptance=result['status'],normal_cli_managed_lifecycle_accepted=True,
        startup_stop_accepted=True,continuous_two_generation_run_accepted=False,
        installed_service=False,previous_package=result['previous_package']))
    names=set(s.check()['sources'])|set(f.check()['sources'])
    for name in names:
        if name.startswith((s.HERE,'tools/serve_service/','tools/serve_ops/')) or 'SERVE_SUPERVISOR_DEADLINES' in name:
            path=s.BASE/'reviewed-source'/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write((ROOT/name).read_bytes())
    archive_base.archive()


if __name__=='__main__':
    os.umask(0o077);configure();p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('preflight','postflight','archive','verify'));action=p.parse_args().action
    if action in ('preflight','postflight'):
        f.check();original.resources('recheck-'+action)
    elif action=='archive':archive()
    else:archive_base.verify()
