"""Finish only the unrun light profile; preserve both primary failed runs.

One model, five WSS sessions, three native TLS entries and 21 native jobs.
Primary has complete actual protocol and clarified-task receipts. Fixed HTTP
seed diagnostics retain their one quality FAIL; they do not claim a quality fix.
"""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import profile_duplex_followup_targeted as prior
from tools.serve_recovery import profile_duplex_followup as evidence
from tools.serve_recovery import profile_duplex as duplex
from tools.serve_recovery import profile_contract,profile_record,profile_evidence
from tools.serve_service.package import read,require,save,sha
full,encrypted,WORK=prior.full,prior.encrypted,prior.WORK
LIMITS=dict(starts=1,chats=21,controls=1500,seconds=900,websocket_sessions=5,ingress_starts=3)
FORMAT='q4t-duplex-light-resume-v1'
SUCCESS='PASS_UNRUN_DUPLEX_LIGHT'


def primary_files():
    return {**prior.original_files(),**{str(WORK/'profiles-02'/n):h for n,h in evidence.FOLLOWUP_REVIEW.items()}}


def primary():
    prior.original_environment();source=WORK/'profiles-02'
    for n,h in primary_files().items():require(sha(n)==h,'primary original evidence changed')
    result=read(source/'result.json');guard=read(source/'lease/exit.json')
    require(result['status']=='FAILED' and result['counts']==dict(starts=1,chats=13,controls=45,
        websocket_sessions=2,ingress_starts=1) and result['error'].endswith('RuntimeError: metadata size limit\n') and
        'cleanup_error' not in result and guard['status']=='COMMAND_FAILED' and guard['child_returncode']==1 and
        guard['compute_backend']=='nvml' and guard['lease_inherited_by_child'] is True,'unreviewed primary follow-up failure')
    queries=[json.loads(l) for l in (source/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(len(queries)==59 and all(q['status']=='OK' and q['returncode']==0 and not q['observed_errors'] for q in queries),'primary follow-up environment')
    events=[json.loads(l) for l in (source/'lease/events.jsonl').read_text().splitlines()]
    clean=read(source/'cleanup.json');entry=read(source/'primary-duplex-entry-cleanup.json')
    require(events[-1]['event']=='lease_released' and not any(e.get('conflicts') for e in events) and
        clean['remaining']==[] and clean['model_exit']==0 and clean['ports_released'] is True and
        clean['lease_continuously_held'] is True and entry['exit_code']==0 and entry['port_released'] is True,
        'primary follow-up cleanup')
    plan=read(source/'plan.json');prior.check_plan(plan)
    first=WORK/'profiles-01'
    value=dict(format=evidence.FORMAT,original=duplex.read_observed(first/'primary-duplex-observed.json'),
        followup=duplex.read_observed(source/'primary-duplex-sessions-observed.json'),
        diagnostics=read(source/'primary-diagnostics.json'),source_review=evidence.REVIEW,
        followup_review=evidence.FOLLOWUP_REVIEW)
    evidence.business(value)
    _,old=prior.first.original(plan,'primary');business=copy.deepcopy(old['business'])
    business.update(realtime_duplex=value,realtime=read(first/'primary-realtime-business.json'),
        semantic_vad=read(first/'primary-semantic-business.json'))
    caps=read(source/'primary-capabilities.json');require(caps==read(first/'primary-capabilities.json'),'primary caps drift')
    profile_evidence.business(business,plan['identity'],plan['profiles']['primary'],caps)
    config=read(source/'primary-config.json')
    return prior.reuse.attach(dict(format=profile_evidence.DUPLEX_FORMAT,profile='primary',identity=plan['identity'],
        capacity=plan['profiles']['primary'],capabilities=caps,
        configuration=prior.first.contracts.configuration(config,version=5),
        startup_memory=config['startup_memory']['checks']['before_allocations'],business=business,
        environment=dict(status='UNQUALIFIED_PENDING_GUARD_EXIT')))


def check_plan(plan):
    old=read(WORK/'profiles-02/plan.json');prior.check_plan(old)
    require(plan['targeted_format']==FORMAT and plan['limits']==LIMITS and
        plan['primary_files']==primary_files() and all(plan[k]==old[k] for k in
        ('binary_sha256','identity','profiles','native_assets','candidate_identity','base','model_assets')) and
        plan['requests']=={} and plan['automatic_retries'] is False and
        plan['duplex']['instruction_policy']==prior.POLICY,'unrun light identity/scope')


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed source')
    cpu=WORK/'profile-primary-assembly-cpu-01';require(read(cpu/'result.json')['status']=='PASS','targeted CPU missing')
    for n,h in read(cpu/'plan.json')['sources'].items():require(sha(ROOT/n)==h,'CPU source drift')
    value=primary();out=args.output.resolve();require(out.is_relative_to(WORK),'private output required')
    out.mkdir(mode=0o700,parents=True);plan=read(WORK/'profiles-02/plan.json')
    plan.update(targeted_format=FORMAT,limits=LIMITS,output=str(out),port=args.port,operations_port=args.port+1,
        scope=__doc__,primary_files=primary_files(),planned={k:v for k,v in LIMITS.items() if k not in ('seconds','controls')},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    plan['sources']={n:sha(ROOT/n) for n in plan['sources']}
    names=subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_realtime'],cwd=ROOT,text=True).splitlines()
    plan['sources'].update({n:sha(ROOT/n) for n in names});plan['sources'].update(primary_files())
    for n in ('plan.json','result.json'):plan['sources'][str(cpu/n)]=sha(cpu/n)
    plan['realtime']=prior.first.profile_realtime.prepare(Path(plan['realtime']['source_plan']),out,args.port+2,plan['sdk'])
    plan['duplex']=duplex.prepare(WORK/'idle-business-01/run/plan.json',out,args.port+2,plan['sdk'])
    plan['duplex']['instruction_policy']=prior.POLICY
    for k in ('realtime','duplex'):plan['sources'].update(plan[k]['files'])
    for n,h in plan['protected'].items():require(sha(n)==h,'default changed')
    path=out/'primary-unqualified.json';profile_record.save(path,value);plan['primary_record_sha256']=sha(path)
    check_plan(plan);save(out/'plan.json',plan);print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(prior.Stage):
    limits=LIMITS
    success_status=SUCCESS
    def __init__(self,plan):
        check_plan(plan);encrypted.Stage.__init__(self,plan)
        self.counts.update(websocket_sessions=0,ingress_starts=0)
    def start(self):full.Stage.start(self,selected='light')
    def business(self):
        require(self.profile_name=='light','primary must not be replayed')
        self.profile_business()
        value=profile_record.read(self.out/'primary-unqualified.json')
        require(sha(self.out/'primary-unqualified.json')==self.plan['primary_record_sha256'],'assembled primary changed')
        profile_contract.capability_pair(value['capabilities'],read(self.out/'light-capabilities.json'))


def finalize(out):
    plan=read(out/'plan.json');check_plan(plan);result=read(out/'result.json')
    require(result['status']==SUCCESS and result['binary_sha256']==plan['binary_sha256'] and
        0<result['seconds']<=900 and set(result['counts'])==set(LIMITS)-{'seconds'} and
        all(type(result['counts'][k]) is int and result['counts'][k]==LIMITS[k] for k in LIMITS if k not in ('seconds','controls')) and
        type(result['counts']['controls']) is int and 0<=result['counts']['controls']<=1500,'unrun light incomplete')
    for n,h in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():require(sha(ROOT/n)==h,'postflight source drift')
    require(sha(plan['binary'])==plan['binary_sha256'],'binary drift');full.bundle.check_assets(plan['model'],plan['model_assets'])
    require(sha(out/'primary-unqualified.json')==plan['primary_record_sha256'] and
        profile_record.read(out/'primary-unqualified.json')==primary(),'primary assembly changed')
    lease=out/'lease';guard=read(lease/'exit.json');queries=[json.loads(l) for l in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    clean=read(out/'cleanup.json')
    require(guard['status']=='COMPLETED' and guard['child_returncode']==0 and guard['compute_backend']=='nvml' and
        guard['lease_inherited_by_child'] is True and queries and all(q['status']=='OK' and q['returncode']==0 and
        not q['observed_errors'] for q in queries),'light strict environment')
    require(clean['remaining']==[] and clean['model_exit']==0 and clean['ports_released'] is True and
        clean['lease_continuously_held'] is True,'light cleanup')
    for kind in ('realtime','semantic','duplex'):
        row=read(out/('light-'+kind+'-entry-cleanup.json'));require(row['exit_code']==0 and row['port_released'] is True,'light entry cleanup')
    process_guard=encrypted.foreground.base.lifecycle.guard;processes=process_guard.processes()
    all_clean=[clean,*[read(WORK/n/'cleanup.json') for n in ('profiles-01','profiles-02')]]
    require(not any(processes.get(int(pid),{}).get('start_ticks')==row['start_ticks'] for c in all_clean for pid,row in c['owned'].items()),'owned process remains')
    full.background.probe_ports((plan['port'],plan['operations_port'],plan['duplex']['public_port']))
    with process_guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    save(out/'environment-composition.json',dict(original=evidence.REVIEW,followup=evidence.FOLLOWUP_REVIEW,
        light_guard=sha(lease/'exit.json'),original_stage_status=['FAILED','FAILED'],
        original_quality_failures_preserved=True,diagnostic_quality_status='FAIL'))
    environment=dict(status='PASS',env_abort=False,all_queries_ok=True,nvml_queries=162+len(queries),
        lease_result_sha256=sha(out/'environment-composition.json'),cleanup_complete=True)
    qualified=out/'qualified';qualified.mkdir(mode=0o700)
    registry=dict(format=profile_contract.FORMAT,identity=plan['identity'],profiles=plan['profiles'],qualifications={})
    for name in profile_contract.NAMES:
        value=profile_record.read(out/(name+'-unqualified.json'));require(value['environment']==dict(status='UNQUALIFIED_PENDING_GUARD_EXIT'),'pending provenance differs')
        value['environment']=environment;path=qualified/(name+'.json');profile_record.save(path,value)
        registry['qualifications'][name]=profile_contract.qualification(name,value['capacity'],value['identity'],value['capabilities'],value['configuration'],value['startup_memory'],sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path=qualified/(name+'.json');profile_evidence.validate(profile_record.read(path),registry,name,sha(path))
    save(qualified/'registry.json',registry)
    save(out/'acceptance.json',dict(status='ACCEPT_DUPLEX_PRIMARY_LIGHT',environment=environment,
        counts=result['counts'],seconds=result['seconds'],registry_sha256=sha(qualified/'registry.json'),
        cumulative_counts=dict(starts=3,native_admissions=55,websocket_sessions=12,ingress_starts=7),
        original_stage_status=['FAILED','FAILED'],original_quality_status='FAIL',diagnostic_quality_status='FAIL',
        actual_clarified_primary_tasks_pass=True,default_unchanged=True,managed_recovery_accepted=False,
        model_or_sampling_changed=False,model_quality_repaired=False,performance_claim=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--output',type=Path,required=True);p.add_argument('--port',type=int,default=18538)
    for mode in ('run','finalize'):sub.add_parser(mode).add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    elif args.mode=='finalize':finalize(args.output.resolve())
    else:
        def interrupted(_signum,_frame):raise TimeoutError('light resume deadline/interruption')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted);signal.alarm(900)
        try:Stage(read(args.output/'plan.json')).run()
        finally:signal.alarm(0)


if __name__=='__main__':main()
