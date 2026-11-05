"""Raw evidence and independent replay for the directed startup protocol handoff."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_current_release import task_stage as s
from tools.project_tasks import store,engine,migration,protocol,retention
from tools.project_tasks import protocol_report as legacy
from tools.serve_service import package as pkg

DEST=ROOT/'docs/evidence/serve-task-current-20261105'
READERS=sorted(set((*s.CPU_SOURCES,'tools/project_tasks/protocol_report.py',
    'tools/serve_error_params/stage.py','tools/serve_clients/short.py')))
require=pkg.require
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def tasks(data, plan, ends):
    """Replay the same contracts, binding the reader to the actual frozen plan."""
    read = lambda name: json.loads(data[name])
    usage, ids = {}, set()
    parent = s.BASE/'offline'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client, tokens in (('alpha', 512), ('beta', 64)):
            known = read('execution/'+client+'-known-ledger.json')
            final = read('execution/'+client+'-ledger.json')
            require(known['meta'] == final['meta'] and known['events'] ==
                final['events'][:len(known['events'])] and known['events'][-1]['kind'] ==
                'tool_result', 'saved history changed')
            prefix = 'execution/'+client+'-completed-task/'
            folder = Path(temp)/final['meta']['task_id']
            folder.mkdir(mode=0o700)
            for name, raw in data.items():
                if name.startswith(prefix): store.write_new(folder/name[len(prefix):], raw)
            require(sha(data[prefix+'ledger.db']) == final['database_sha256'], 'ledger identity')
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, final['meta']['store_id'])
                state = engine.replay(ledger)
                public = engine.public(ledger, state)
                require(ledger.meta == final['meta'] and ledger.events() == final['events'] and
                    ledger.spec['runtime_sources'] == plan['historical']['sources'] and
                    ledger.spec['binding'] == plan['bindings'][client+'-old'] and
                    state['execution_spec']['binding'] == plan['bindings'][client+'-new'] and
                    state['execution_spec']['runtime_sources'] ==
                    {n:plan['source_files'][n] for n in engine.RUNTIME_FILES}, 'runtime/protocol binding')
                expected = json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(public == expected and public['state'] == 'COMPLETED' and
                    public['outcome'] == 'ANSWERED' and public['requests'] == 2 and
                    public['tool_attempts'] == public['saved_tool_results'] == public['migrations'] == 1 and
                    public['max_tokens'] == tokens and 'Qwen4ExpForConditionalGeneration' in public['answer'] and
                    '[docs/MODEL.md:L1-L24]' in public['answer'], 'answer/budgets')
                by = lambda kind:[row['payload'] for row in ledger.events() if row['kind'] == kind]
                change = by('migration')[0]['plan']
                require(change['format'] == migration.PROTOCOL_FORMAT and
                    change['source_release'] == plan['historical']['release'] and
                    change['protocol_review'] == plan['protocol_review'], 'explicit reviewed pair')
                protocol.validate(change['protocol_review'], change['source_spec']['binding'],
                                  change['target_spec']['binding'])
                require([row['protocol_source_sha256'] for row in by('preflight_result')] ==
                        [v['protocol'] for v in plan['versions']], 'old/new actual preflight')
                require(by('tool_started')[0]['name'] == 'read_project_record' and
                    by('tool_started')[0]['arguments'] ==
                    dict(path='docs/MODEL.md', start_line=1, line_count=24), 'actual tool arguments')
                returned = json.loads(by('tool_result')[0]['content'])
                require(returned['ok'] and returned['source']['sha256'] ==
                        plan['source_files']['docs/MODEL.md'], 'actual source content')
                observations = [json.loads(raw) for raw in
                    data['execution/'+client+'-old/read-observations.jsonl'].splitlines()]
                require(len(observations) == 1 and observations[0]['invocation']['name'] ==
                        'read_project_record', 'exactly one real tool execution')
                usage[client] = [row['completion']['usage'] for row in by('response')]
                require(len(usage[client]) == 2 and all(0 < row['completion_tokens'] <= tokens
                    for row in usage[client]), 'output bounds')
                ids.update(row['completion']['id'] for row in by('response'))
                require(json.loads(read('execution/'+client+'-duplicate-apply.json')['stdout'])['already_applied'] and
                    json.loads(read('execution/'+client+'-duplicate-submit.json')['stdout']) ==
                    dict(public, created=False), 'active duplicate')
                key = read('execution/'+client+'-archive-key.json')
                archived = retention.key_state(key, read('tasks/store.json'), ledger.meta['task_id'])
                require(archived['binding'] == plan['bindings'][client+'-new'] and archived['rekeys'] == 0 and
                    archived['payload'] == ('retained' if client == 'alpha' else 'purged') and
                    key['base']['contract_sha256'] == retention.business_contract(state['execution_spec']) and
                    key['base']['answer_sha256'] == store.digest(public['answer']), 'permanent key identity')
                for label in ('archive-duplicate-submit', 'archive-resume'):
                    value = json.loads(read('execution/'+client+'-'+label+'.json')['stdout'])
                    require(value['state'] == 'ARCHIVED' and value['no_execution'] and
                            value['payload_state'] == archived['payload'], 'archive no execution')
                prefix = 'tasks/retention/payloads/'+ledger.meta['task_id']+'/'
                if client == 'alpha':
                    require(all(sha(data[prefix+name]) == row['sha256'] for name,row in archived['files'].items()),
                            'retained payload bytes')
                else:
                    require(not any(name.startswith(prefix) for name in data), 'pruned payload retained')
            finally:
                db.close()
    require(ids == {row['id'] for row in ends}, 'backend response correlation')
    return usage


def public_bytes(raw,secrets):
    return all(secret not in raw for secret in secrets) and not re.search(
        rb'(?m)^-----BEGIN [A-Z ]*PRIVATE KEY-----\r?$',raw)


def collect():
    plan=s.check();data={}
    for name in ('plan.json','run.stdout','run.stderr'):
        data[name]=(s.BASE/name).read_bytes()
    for folder in ('cpu','execution','postflight','plans'):
        for path in sorted((s.BASE/folder).rglob('*')):
            if path.is_file():
                pkg.regular(path);data[str(path.relative_to(s.BASE))]=path.read_bytes()
    for name in plan['historical']['sources']:
        data['historical-source/'+name]=(s.HISTORICAL/name).read_bytes()
    for path in sorted(s.TASKS.rglob('*')):
        if path.is_file():
            pkg.regular(path);data['tasks/'+str(path.relative_to(s.TASKS))]=path.read_bytes()
    for name in READERS:data['current-source/'+name]=(ROOT/name).read_bytes()
    data['review/manifest.json']=(s.review.DEST/'manifest.json').read_bytes()
    data['review/baseline-refusal.json']=(s.BASE/'review/baseline-refusal.json').read_bytes()
    front=pkg.read(plan['frontend']);secrets=[c['api_key'].encode() for c in front['clients']]
    secrets.append(pkg.read(plan['policy'])['gateway_token'].encode())
    for name,raw in data.items():require(public_bytes(raw,secrets),'private credential or PEM block: '+name)
    return data


def audit(data):
    read=lambda name:json.loads(data[name])
    plan,result,cpu=(read(n) for n in ('plan.json','execution/result.json','cpu/result.json'))
    require(cpu['passed'] and cpu['tests_run']==len(set(cpu['names']))>=20 and
        cpu['errors']==cpu['failures']==cpu['skipped']==0 and
        len(cpu['file_operation_kills'])==4 and sha(data['cpu/result.json'])==plan['cpu_sha256'],'CPU evidence')
    for name,digest in cpu['sources'].items():
        require(sha(data['cpu/source/'+name])==digest==plan['source_files'][name],'CPU source identity')
    for name in READERS:
        require(sha(data['current-source/'+name])==plan['source_files'][name],'reader identity')
    for name,digest in plan['historical']['sources'].items():
        require(sha(data['historical-source/'+name])==digest,'prior application source')
    require(plan['historical']['release']=='srv11k-f4705009' and
        plan['historical_archive']==s.review.HISTORY and
        pkg.sha(ROOT/plan['historical_archive'])==plan['historical_archive_sha256']==s.review.HISTORY_SHA,
        'accepted prior source archive')
    proof=read('review/manifest.json')
    require(proof['archive_sha256']==plan['protocol_review']['archive_sha256'] and
        pkg.sha(ROOT/plan['protocol_review']['evidence'])==proof['archive_sha256'] and
        proof['review']['from_protocol']==s.review.BEFORE and proof['review']['to_protocol']==s.review.AFTER and
        proof['review']['unchanged_runtime_files']==214 and proof['review']['identical_schemas']==7 and
        read('review/baseline-refusal.json')['code']=='protocol_unsupported','independent compatibility review')
    require(result['status']=='PASS_TASK_PROTOCOL_SHORT' and result['cleanup_confirmed'] and
        result['counts']['starts']==2 and result['counts']['generation_attempts']==4 and
        result['counts']['controls']<=80 and result['seconds']<=270 and not result['long_run'],'bounded real result')
    ends=[]
    for label in ('old','new'):
        log=data[f'execution/{label}/scoped-model/stderr.log'].decode()
        rows=[dict(re.findall(r'([a-z_]+)=([^ ]+)',line)) for line in log.splitlines()
            if line.startswith('[q4t][request]') and ' event=end ' in line]
        require(len(rows)==2 and all(r['outcome']=='success' and r['reason']=='completed' and
            r['released']=='1' for r in rows) and log.count('grammar worker reaped=1 ')==2 and
            'shutdown complete (0 in-flight remaining)' in log,'backend completion/cleanup')
        ends.extend(rows);metrics=read('execution/'+label+'-metrics.json')
        require(metrics['q4t_requests_success_total']==metrics['q4t_structured_workers_started_total']==
            metrics['q4t_structured_workers_reaped_total']==2 and
            all(metrics[n]==0 for n in s.prior.s.a.EMPTY),'phase resources')
        for component in ('scoped-model','nginx'):
            require(read(f'execution/{label}/{component}/reaped.json')['returncode']==0,'service child exit')
    require(read('execution/old-stopped.json')['monotonic']<
        read('execution/new/scoped-model/identity.json')['registered_monotonic'],'backend overlap')
    for client in ('alpha','beta'):
        require(read('execution/'+client+'-old/reaped.json')['returncode']==-9,'application interruption')
        for label,code in (('unmigrated','binding_changed'),('wrong-operation','plan_changed'),
                           ('archive-reverse','protocol_unsupported')):
            value=read('execution/'+client+'-'+label+'.json')
            require(value['returncode']==1 and json.loads(value['stderr'])['code']==code,'metadata refusal')
    usage=tasks(data,plan,ends)
    require(read('execution/capability-difference.json')==dict(changed=['protocol_source_sha256']),
            'actual public capability change')
    for index,expected in enumerate((s.review.BEFORE,s.review.AFTER)):
        require(read(f'execution/capabilities-{index}.json')['protocol_source_sha256']==expected,'actual versions')
    state=read('execution/retention-status.json')
    require((state['active_tasks'],state['archived_keys'],state['retained_payloads'])==(0,2,1),'permanent keys')
    events=[json.loads(raw) for raw in data['execution/lease/events.jsonl'].splitlines()]
    begin=[v for v in events if v['event']=='lease_acquired'];end=[v for v in events if v['event']=='lease_released']
    observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(len(begin)==len(end)==1 and end[0]['status']==read('execution/lease/exit.json')['status']=='COMPLETED' and
        observations and all(not v['conflicts'] and v['unreadable_processes']==0 for v in observations),'GPU lease')
    seconds=end[0]['monotonic']-begin[0]['monotonic'];require(seconds<=270,'GPU bound')
    post=read('postflight/result.json')
    require(post['passed'] and len(post['owners'])==6 and all(v['gone'] for v in post['owners']) and
        post['lock']==dict(dev=66305,inode=3294067,released=True) and read('postflight/gpu.json')['pids']==[],
        'postflight resources')
    return dict(status='ACCEPT_TASK_CURRENT_PROTOCOL',cpu_tests=cpu['tests_run'],file_operation_kills=4,
        models=2,gateways=2,responses=4,real_tools=2,application_kills=2,active_migrations=2,
        duplicate_applies=2,duplicate_submits=4,archive_resumes_without_execution=2,metadata_refusals=6,
        archived_protocol_migrations_cpu_only=True,source_only_18_file_migration_cpu_only=True,source_files_before=17,source_files_after=18,
        permanent_keys=2,retained_payloads=1,usage=usage,seconds=result['seconds'],controls=result['counts']['controls'],
        gpu_seconds=seconds,guard_observations=len(observations),owned_exited=6,default_unchanged=True,
        runtime_sources_unchanged=223,reviewed_transition=plan['protocol_review'],
        source_commit=plan['source_commit'],long_run=False,actual_lan_accepted=False,
        physical_ram_limit=False,performance_acceptance=False)


def seal():
    s.check();data=collect();result=audit(data);DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz','w:gz') as archive:
        for name,raw in sorted(data.items()):
            member=tarfile.TarInfo(name);member.mode=0o400;member.size=len(raw)
            archive.addfile(member,io.BytesIO(raw))
    pkg.save(DEST/'result.json',result);plan=json.loads(data['plan.json'])
    pkg.save(DEST/'manifest.json',dict(records={n:sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'),result_sha256=pkg.sha(DEST/'result.json'),
        readers={n:pkg.sha(ROOT/n) for n in READERS},
        binaries={v['binary']:v['binary_sha256'] for v in plan['versions']}))


def verify():
    manifest=pkg.read(DEST/'manifest.json')
    require(pkg.sha(DEST/'records.tar.gz')==manifest['archive_sha256'] and
        pkg.sha(DEST/'result.json')==manifest['result_sha256'],'sealed archive identity')
    with tarfile.open(DEST/'records.tar.gz') as archive:
        members=archive.getmembers()
        require(len(members)==len(manifest['records']) and len({m.name for m in members})==len(members) and
            all(m.isfile() and m.name in manifest['records'] and m.size<=16*1024**2 and
                not Path(m.name).is_absolute() and '..' not in Path(m.name).parts for m in members),'archive inventory')
        data={m.name:archive.extractfile(m).read() for m in members}
    require({n:sha(raw) for n,raw in data.items()}==manifest['records'],'raw records differ')
    for name,digest in {**manifest['readers'],**manifest['binaries']}.items():
        require(pkg.sha(ROOT/name)==digest,'local source/binary changed: '+name)
    require(audit(data)==pkg.read(DEST/'result.json'),'reconstruction differs')
    return dict(status='PASS_ARCHIVE',records=len(data),binaries=len(manifest['binaries']))


if __name__=='__main__':
    os.umask(0o077);s.configure();legacy.s=s.prior
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('postflight','seal','verify'))
    action=p.parse_args().action
    if action=='postflight':legacy.postflight()
    elif action=='seal':seal();print(json.dumps(verify()))
    else:print(json.dumps(verify()))
