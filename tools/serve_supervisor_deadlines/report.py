"""Offline reconciliation and secret-screened records for deadline acceptance."""
import argparse
from dataclasses import asdict
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_supervisor_deadlines import stage as s
from tools.serve_operations_managed import report as archive_base

ARCHIVE=ROOT/'docs/evidence/serve-supervisor-deadlines-20261104'
BASE_EXPORTED=archive_base.exported
UNIT='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
pkg=s.pkg


def resources(label):
    out=s.BASE/label;out.mkdir(mode=0o700)
    s.base.client.a.require_idle(out/'gpu.json')
    lock=s.service.guard.common_lock();stat=lock.stat()
    pkg.require((stat.st_dev,stat.st_ino)==(66305,3294067),'original lock changed')
    with lock.open('r+') as probe:
        fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
    ports=(s.base.PORT,s.base.OPS_PORT,s.base.TLS_PORT)
    pkg.require(not any(s.base.inspection.listeners(p) for p in ports),'listener retained')
    raw=subprocess.check_output(['systemctl','--user','show',UNIT,'-p','LoadState',
        '-p','ActiveState','-p','SubState','-p','MainPID'],text=True)
    (out/'soar-unit.txt').write_text(raw)
    pkg.require(dict(line.split('=',1) for line in raw.splitlines() if '=' in line)==
        dict(MainPID='0',LoadState='masked',ActiveState='inactive',SubState='dead'),'restore unit changed')
    raw=subprocess.check_output(['sudo','-n','docker','ps','--format','{{.ID}} {{.Names}}'],text=True)
    (out/'containers.txt').write_text(raw)
    pkg.require(not raw.strip(),'running container')
    s.check()
    pkg.save(out/'result.json',dict(passed=True,gpu_empty=True,ports=list(ports),
        lock=dict(available=True,dev=stat.st_dev,inode=stat.st_ino),
        containers_empty=True,restore_unit_masked=True,long_runs=False))


def audit():
    plan=s.check();root=s.OUT
    result=pkg.read(root/'result.json')
    pkg.require(result['status']=='PASS_SUPERVISOR_DEADLINES_SHORT' and
        result['cleanup_confirmed'] and result['counts']==
        dict(starts=2,generation_attempts=4,controls=16),'bounded driver result')
    original=pkg.read(s.BASE/'baseline/provenance.json')
    pkg.require(original['observed_failures']==3 and original['observed_errors']==0 and
        pkg.read(s.BASE/'baseline/result.json')['returncode']==1 and
        'FAILED (failures=3)' in (s.BASE/'baseline/unittest.stderr').read_text(),
        'original failures missing')
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
    before=pkg.read(root/'startup-before-stop.json');stop=pkg.read(root/'startup-stop-result.json')
    pkg.require(s.startup_snapshot(before['state'],'',before['peers']) and
        stop['passed'] and stop['seconds']<plan['startup_stop_seconds'] and
        stop['no_ready_publication'] and stop['same_lease'] and
        stop['terminal_sha256']==pkg.sha(root/'startup-run/terminal.json'),'warming stop')
    pkg.require('[q4t] serving on port' not in (root/'startup-run/backend.log').read_text() and
        not any(r.endswith((' READY',' DEGRADED')) for r in
            (root/'startup-run/events.log').read_text().splitlines()),'startup published ready')
    owners += [dict(role='startup-controller',**pkg.read(root/'startup-controller-identity.json')),
        dict(role='startup-worker',**before['state']['owner']),
        dict(role='startup-backend',**before['state']['backend']),
        dict(role='transport-probes',**pkg.read(root/'transport-probes-identity.json'))]
    leases=[];polls=[]
    for name,external in (('managed-run',False),('startup-run',True)):
        terminal=pkg.read(root/name/'terminal.json')
        pkg.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and
            terminal['worker']['replayed_requests']==0 and
            terminal['guard']['status']=='COMPLETED' and
            terminal['guard']['external_lease']==external,'controller terminal')
        events=[json.loads(line) for line in (root/name/'lease/events.jsonl').read_text().splitlines()]
        observations=[r for r in events if r['event']=='monitor']
        pkg.require(events[0]['event']=='lease_acquired' and
            events[-1]['event']==('lease_guard_finished' if external else 'lease_released') and
            observations and all(not r['conflicts'] and r['unreadable_processes']==0 for r in observations),
            'guard observations')
        leases.append(events);polls.append(len(observations))
        final=pkg.read(root/(name+'-stopped-inspection.json'))
        pkg.require(final['complete'] and final['cleanup_observed'] and not final['listeners'] and
            not final['gpu']['pids'] and final['lock']['state']=='AVAILABLE','final inspection')
    copy=pkg.read(root/'custody/lease-copy.json')
    custody=[json.loads(line) for line in (root/'custody/events.jsonl').read_text().splitlines()]
    pkg.require(copy['device']==66305 and copy['inode']==3294067 and
        copy['owner']==pkg.read(root/'controller-identity.json') and
        all(not r['observation']['conflicts'] and r['observation']['unreadable_processes']==0 for r in custody) and
        pkg.read(root/'custody/exit.json')['status']=='COMPLETED' and
        leases[0][0]['monotonic']<=custody[0]['monotonic']<leases[0][-1]['monotonic'] and
        leases[0][-1]['monotonic']<=leases[1][0]['monotonic']<leases[1][-1]['monotonic']<=custody[-1]['monotonic'],
        'continuous original lease custody')
    seconds=custody[-1]['monotonic']-leases[0][0]['monotonic']
    pkg.require(seconds<s.GPU_SECONDS and result['seconds']<s.LIMITS['seconds'],'time bounds')
    transport=pkg.read(root/'transport/result.json')
    pkg.require(transport['passed'] and transport['requests']==len(transport['rows'])==3 and
        transport['transport_sha256']==pkg.sha(ROOT/'tools/serve_ops/transport.py'),'sealed transport')
    for row,expected in zip(transport['rows'],('observation_stopped','http_deadline',None)):
        pkg.require(row['reason']==expected and row['seconds']<.7 and row['peer_closed'],'transport bound')
    for owner in owners:
        pkg.require(not s.base.gateway.alive({k:owner[k] for k in ('pid','start_ticks','boot_id')}),'owned identity remains')
    pkg.require(pkg.read(s.BASE/'postflight/result.json')['passed'],'resource postflight')
    return dict(status='ACCEPT_SUPERVISOR_DEADLINES_SHORT',package=plan['package'],
        previous_package=plan['previous_package'],source_commit=plan['source_commit'],
        default_sha256=pkg.sha(ROOT/'build/q4t'),sidecar_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        protocol=plan['protocol'],cpu_checks=114,
        driver_checks=pkg.read(s.BASE/'driver-cpu/result.json')['tests_run'],original_failures=3,
        backend_attempts=2,complete_models=1,startup_stops=1,startup_stop_seconds=stop['seconds'],
        successful_responses=4,real_tools=2,responses=responses,transport=transport,
        gateway_starts=2,rejected_gateways=2,public_connections_at_new_supervisor_sample=136,
        parent_controls=result['counts']['controls'],supervisor_http_upper_bound=plan['supervisor_http_upper_bound'],
        guard_observations=polls,custody_observations=len(custody),
        monitored_shared_lock_seconds=seconds,driver_seconds=result['seconds'],owned_processes=owners,
        release_declarations_verified_live=True,cleanup_observed=True,default_changed=False,
        validation_bridge='second controller sealed service.run with existing borrowed guard API',
        host_envelope_enabled_in_package=False,physical_ram_hard_limit_proved=False,
        long_runs_executed=False,actual_lan_accepted=False,persistent_installation=False,
        performance_acceptance=False)


def originals():
    excluded={'archive.stdout','archive.stderr','verify.stdout','verify.stderr'}
    return sorted(p for p in s.BASE.rglob('*') if p.is_file() and p.name not in excluded)


def exported(path):
    relative=path.relative_to(s.BASE)
    if relative.parts[0] in ('baseline','reviewed-source'):return True
    if path.suffix=='.txt' and path.parent.name in ('preflight','postflight'):return True
    return BASE_EXPORTED(path)


def configure():
    s.configure()
    archive_base.ARCHIVE,archive_base.audit,archive_base.exported=ARCHIVE,audit,exported
    archive_base.original_files=originals


def archive():
    plan=s.check()
    for name in plan['sources']:
        if name.startswith((s.HERE,'tools/serve_service/','tools/serve_ops/')) or name.endswith('SERVE_SUPERVISOR_DEADLINES_2026-11-04.md'):
            path=s.BASE/'reviewed-source'/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write((ROOT/name).read_bytes())
    archive_base.archive()


if __name__=='__main__':
    os.umask(0o077);configure()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('preflight','postflight','archive','verify'))
    action=p.parse_args().action
    if action in ('preflight','postflight'):resources(action)
    elif action=='archive':archive()
    else:archive_base.verify()
