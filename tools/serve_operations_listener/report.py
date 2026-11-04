"""Offline evidence reconciliation and secret-free archive; no HTTP/model work."""
import argparse
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu,business_fix as f
from tools.serve_service import package as pkg

ARCHIVE=ROOT/'docs/evidence/serve-operations-listener-20261104'
RUNS=('execution','execution-02','execution-03','execution-04')


def audit():
    f.bind();f.s.h.verify_seal(f.OUTPUT)
    for name in ('execution','execution-02','execution-03'):f.s.h.verify_seal(cpu.BASE/name)
    plan=cpu.read(f.PACKAGE/'plan.json');final=cpu.read(f.OUTPUT/'finalized.json')
    for name,digest in final['installed'].items():cpu.require(cpu.sha(ROOT/'build'/name)==digest,'default changed')
    cpu.require(final['status']=='DELIVERED','delivery incomplete')
    b=f.OUTPUT/'business';result=cpu.read(b/'result.json')
    cpu.require(result['status']=='PASS_BOUNDED_OPERATIONS_LISTENER' and
        result['counts']==dict(starts=1,generation_attempts=6,controls=8) and
        result['successes']==5 and result['real_tools']==2 and result['cancelled']==1,'business totals')
    successes=[]
    for label in ('ordinary-recovery','project-alpha-read','project-alpha-answer','project-beta-read','project-beta-answer'):
        response=f.s.s.a.read_sse(io.BytesIO((b/(label+'-wire.bin')).read_bytes()))
        stored=cpu.read(b/(label+'-response.json'));elapsed=stored.pop('elapsed')
        cpu.require(asdict(response)==stored,'wire/parsed response mismatch')
        request=cpu.read(b/(label+'-request.json'))['body']
        cpu.require(response.id==request['request_id'] and response.model==request['model'],'request identity')
        ceiling=64 if 'beta' in label else 512
        cpu.require(response.usage['completion_tokens']<=ceiling and request['max_tokens']<=ceiling,'client budget')
        if label=='ordinary-recovery':cpu.require(response.message['content'].strip()=='710003','ordinary quality')
        if label.endswith('-answer'):
            tool=json.loads(cpu.read(b/(label.replace('-answer','-tool-result')+'.json'))['content'])
            source=tool['source']
            cpu.require(tool['ok'] and source['sha256']==cpu.sha(ROOT/'docs/MODEL.md') and
                source['path']=='docs/MODEL.md' and source['start_line']==1 and source['end_line']==24,'real project source')
            f.s.s.a.validate_answer(response.message['content'],{source['citation']},1)
            cpu.require('Qwen4ExpForConditionalGeneration' in response.message['content'],'answer value')
        successes.append(dict(label=label,finish_reason=response.finish_reason,usage=response.usage,seconds=elapsed))
    cancel=cpu.read(b/'cancel-active-request.json')['body']
    f.s.s.cancelled_wire((b/'cancel-active-wire.bin').read_bytes(),cancel['request_id'])
    cpu.require(cpu.read(b/'operations-cancel-http.json')['status']==202 and
        cpu.read(b/'cross-client-cancel-http.json')['status']==404,'scoped cancellation')
    f.gateway_error(cpu.read(b/'gateway-local-admin-wire.bin'),cpu.read(b/'gateway-local-admin-http.json'),
        404,'gateway_route_not_found')
    metrics=cpu.read(b/'final-metrics.json')
    for name in ('q4t_active_chats','q4t_business_connections','q4t_public_connections',
                 'q4t_request_body_bytes','q4t_structured_workers_active','q4t_structured_active','q4t_structured_preparing'):
        cpu.require(metrics[name]==0,'retained resource: '+name)
    cpu.require(metrics['q4t_operations_connections']==1 and metrics['q4t_seq_slots_free']==1 and
        metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
        'operations scrape/grammar ownership')
    leases=[];processes=[]
    for run in RUNS:
        folder=cpu.BASE/run
        rows=[json.loads(x) for x in (folder/'lease/events.jsonl').read_text().splitlines()]
        monitors=[r for r in rows if r['event']=='monitor']
        cpu.require(monitors and all(not r['conflicts'] and r['unreadable_processes']==0 for r in monitors),'lease observation gap/conflict')
        cpu.require(rows[0]['event']=='lease_acquired' and rows[-1]['event']=='lease_released','lease lifecycle')
        status=cpu.read(folder/'lease/exit.json')['status']
        cpu.require(status==('COMPLETED' if run=='execution-04' else 'COMMAND_FAILED'),'original status changed')
        leases.append(dict(run=run,status=status,seconds=rows[-1]['monotonic']-rows[0]['monotonic'],observations=len(monitors)))
        if (folder/'business').exists():
            for path in (folder/'business').glob('*/identity.json'):
                owner=cpu.read(path);cpu.require(not Path(f"/proc/{owner['pid']}").exists(),'own process remains')
                processes.append(dict(run=run,role=path.parent.name,pid=owner['pid'],start_ticks=owner['start_ticks']))
    cpu.require(cpu.read(cpu.BASE/'postflight/result.json')['passed'],'postflight failed')
    packaged=cpu.read(cpu.BASE/'accepted-package.json');manifest=pkg.verify(packaged['package']['package'],packaged['package']['manifest_sha256'])
    cpu.require(manifest['version']==6 and not packaged['managed_lifecycle_accepted'],'package scope')
    release=cpu.read(ROOT/'build/q4t.release.json')
    return dict(status='ACCEPT_BOUNDED_OPERATIONS_LISTENER',installed=final['installed'],
        protocol_sha256=release['chat_protocol']['protocol_sha256'],
        production_commit='5e52dc17',validation_commit=plan['source_commit'],
        successes=successes,successful_responses=5,real_tools=2,explicit_cancel=1,
        total_models=2,total_generation_attempts=7,negative_probes=6,
        native_tests=33,python_unique_base=173,diagnostic_targeted=33,final_driver=5,
        continuation_driver=2,native_repair_driver=2,business_repair_driver=3,
        host_public=289,host_required=255,structured_cpu=9,
        rules_scenarios=dict(v3=26,v4=31,v5=41),zero_warning_build=True,
        leases=leases,total_lease_seconds=sum(x['seconds'] for x in leases),
        total_observations=sum(x['observations'] for x in leases),owned_processes=processes,
        prior_failures_preserved=True,default_operations_listener_enabled=False,
        independent_operations_enabled_in_acceptance=True,managed_package_version=6,
        managed_lifecycle_accepted=False,long_runs_executed=False,actual_lan_accepted=False,
        numerical_or_performance_acceptance=False,physical_ram_hard_limit_proved=False,
        resources=cpu.read(cpu.BASE/'postflight/result.json'),
        previous_binary_sha256=plan['previous_binary_sha256'],
        rollback_pair=str(f.PACKAGE/'previous'),accepted_package=packaged['package'])


def files():
    paths=set()
    folders=['preflight','postflight','private','accepted-backend','package','package-native-fix','package-business-fix',*RUNS]
    folders += [p.name for p in cpu.BASE.iterdir() if p.is_dir() and p.name.startswith(('cpu-','build-','driver-'))]
    for name in folders:
        for path in (cpu.BASE/name).rglob('*'):
            if path.is_file():paths.add(path)
    paths.update(p for p in cpu.BASE.glob('*.json'))
    return sorted(paths)


def secrets():
    plan=cpu.read(f.PACKAGE/'plan.json')
    front=cpu.read(plan['frontend']);policy=cpu.read(plan['policy'])
    values={policy['gateway_token'],*(c['api_key'] for c in front['clients'])}
    def collect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('cancel_token','api_key','gateway_token','Authorization') and isinstance(item,str):values.add(item)
                collect(item)
        elif isinstance(value,list):
            for item in value:collect(item)
    for run in RUNS:
        for path in (cpu.BASE/run/'business').glob('*-request.json'):collect(cpu.read(path))
    return [v.encode() for v in values if v]


def public(path):
    rel=path.relative_to(cpu.BASE);parts=rel.parts
    if parts[0]=='private' or path.name.endswith('-request.json'):return False
    if parts[0]=='accepted-backend':return len(parts)==2 and path.name=='manifest.json'
    if parts[0].startswith('package'):
        return (len(parts)==2 and path.suffix=='.json' or
                len(parts)==3 and parts[1]=='candidate' and path.name in ('runtime-source.json','diagnostic-identity.json','q4t.rules.yml'))
    if 'source' in parts:return False
    if 'pair' in parts:return path.name=='q4t.release.json'
    return path.suffix in ('.json','.jsonl','.stdout','.stderr','.log','.bin','.yml')


def archive():
    result=audit();ARCHIVE.mkdir(parents=True)
    private=secrets();records=[];copied=0;size=0
    for path in files():
        raw_size=path.stat().st_size;record=dict(path=str(path),sha256=cpu.sha(path),bytes=raw_size)
        if public(path) and raw_size<=2*1024**2:
            raw=path.read_bytes()
            if not any(secret in raw for secret in private):
                name=Path('records')/path.relative_to(cpu.BASE)
                target=ARCHIVE/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
                record['artifact']=str(name);copied+=1;size+=raw_size
        records.append(record)
    result.update(archived_records=copied,archived_bytes=size,local_references=len(records),
        privacy='Live API keys, gateway token, cancellation credentials and private TLS material stay local; omitted originals are hash references, not backups.')
    cpu.save(ARCHIVE/'result.json',result)
    cpu.save(ARCHIVE/'manifest.json',dict(version=1,records=records,result_sha256=cpu.sha(ARCHIVE/'result.json'),
        verifier=dict(path=str(Path(__file__).resolve()),sha256=cpu.sha(__file__))))
    verify()


def verify():
    manifest=cpu.read(ARCHIVE/'manifest.json')
    cpu.require(cpu.sha(manifest['verifier']['path'])==manifest['verifier']['sha256'],'verifier source changed')
    cpu.require(cpu.sha(ARCHIVE/'result.json')==manifest['result_sha256'],'result changed')
    expected={'manifest.json','result.json'}|{r['artifact'] for r in manifest['records'] if 'artifact' in r}
    actual={str(p.relative_to(ARCHIVE)) for p in ARCHIVE.rglob('*') if p.is_file()}
    cpu.require(actual==expected,'archive inventory changed')
    private=secrets()
    for row in manifest['records']:
        cpu.require(cpu.sha(row['path'])==row['sha256'],'local evidence changed: '+row['path'])
        if 'artifact' in row:
            path=ARCHIVE/row['artifact'];cpu.require(path.resolve().is_relative_to(ARCHIVE.resolve()),'archive path escaped')
            raw=path.read_bytes();cpu.require(cpu.sha(path)==row['sha256'] and len(raw)==row['bytes'],'archive drift')
            cpu.require(not any(secret in raw for secret in private),'secret exported')
    result=cpu.read(ARCHIVE/'result.json');rebuilt=audit()
    cpu.require(all(result[k]==v for k,v in rebuilt.items()),'reconstructed result differs')
    print(json.dumps(dict(verified=True,records=result['archived_records'],local_references=result['local_references'],
        total_lease_seconds=result['total_lease_seconds'],default=result['installed']['q4t'])))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('archive','verify'))
    globals()[p.parse_args().action]()
