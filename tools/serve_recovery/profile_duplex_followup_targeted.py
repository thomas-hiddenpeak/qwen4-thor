"""Target two primary quality failures plus the unrun light profile.

Two models, seven WSS sessions, four TLS entries, 34 native admissions: eight
fixed-seed Chat comparisons, five targeted primary jobs and 21 light jobs.
Original primary protocol/ASR observations stay immutable; no runtime change.
"""
import argparse
import copy
import fcntl
import http.client
import json
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import profile_duplex_targeted as first
from tools.serve_recovery import profile_duplex_followup as evidence
from tools.serve_recovery import profile_duplex,profile_duplex_reuse as reuse
from tools.serve_recovery import profile_contract,profile_evidence,profile_record
from tools.serve_realtime.duplex_quality_checks import POLICY
from tools.serve_service.package import read,require,save,sha
full,encrypted=first.full,first.encrypted
WORK=ROOT/'.q4t-work/serve-realtime-duplex-20261120'
LIMITS=dict(starts=2,chats=34,controls=2000,seconds=1200,websocket_sessions=7,ingress_starts=4)
FORMAT='q4t-duplex-primary-quality-light-v1'
SUCCESS='PASS_DUPLEX_PRIMARY_FOLLOWUP_AND_LIGHT'


def original_files():
    return {str(WORK/('profile-prompt-01' if n.startswith('prompt/') else 'profiles-01')/
        (n[7:] if n.startswith('prompt/') else n)):h for n,h in evidence.REVIEW.items()}


def original_environment():
    for n,h in original_files().items():require(sha(n)==h,'original failure/provenance changed')
    original=WORK/'profiles-01';plan=read(original/'plan.json');first.check_plan(plan)
    result=read(original/'result.json');guard=read(original/'lease/exit.json')
    require(result['status']=='FAILED' and result['counts']==dict(starts=1,chats=21,
        controls=45,websocket_sessions=5,ingress_starts=3) and
        result['error'].endswith('duplex original model quality failed\n') and
        'cleanup_error' not in result and guard['status']=='COMMAND_FAILED' and
        guard['child_returncode']==1 and guard['compute_backend']=='nvml' and
        guard['lease_inherited_by_child'] is True, 'unreviewed original stage failure')
    queries=[json.loads(l) for l in (original/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(len(queries)==103 and all(q['status']=='OK' and q['returncode']==0 and
        not q['observed_errors'] for q in queries),'original environment failed')
    events=[json.loads(l) for l in (original/'lease/events.jsonl').read_text().splitlines()]
    clean=read(original/'cleanup.json')
    require(events[-1]['event']=='lease_released' and not any(e.get('conflicts') for e in events) and
        clean['remaining']==[] and clean['model_exit']==0 and clean['ports_released'] is True and
        clean['lease_continuously_held'] is True, 'original resources not cleaned')
    for kind in ('realtime','semantic','duplex'):
        entry=read(original/('primary-'+kind+'-entry-cleanup.json'))
        require(entry['exit_code']==0 and entry['port_released'],'original TLS cleanup')
    return plan


def check_plan(plan):
    old=original_environment()
    require(plan['targeted_format']==FORMAT and plan['limits']==LIMITS and
        plan['requests']=={} and plan['automatic_retries'] is False and
        plan['duplex']['instruction_policy']==POLICY and
        all(plan[k]==old[k] for k in ('binary_sha256','identity','profiles','native_assets',
            'candidate_identity','base','model_assets')), 'targeted identity/scope differs')
    require(plan['original_files']==original_files(),'original evidence binding differs')


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed source')
    cpu=WORK/'profile-followup-cpu-02';require(read(cpu/'result.json')['status']=='PASS','targeted CPU missing')
    for n,h in read(cpu/'plan.json')['sources'].items():require(sha(ROOT/n)==h,'CPU source drift')
    old=original_environment();out=args.output.resolve();out.mkdir(mode=0o700,parents=True)
    plan=copy.deepcopy(old)
    plan.update(targeted_format=FORMAT,limits=LIMITS,output=str(out),port=args.port,
        operations_port=args.port+1,scope=__doc__,original_files=original_files(),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        planned={k:v for k,v in LIMITS.items() if k not in ('seconds','controls')})
    plan['sources']={n:sha(ROOT/n) for n in plan['sources']}
    names=subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_realtime'],cwd=ROOT,text=True).splitlines()
    plan['sources'].update({n:sha(ROOT/n) for n in names})
    plan['sources'].update(original_files())
    for n in ('plan.json','result.json'):plan['sources'][str(cpu/n)]=sha(cpu/n)
    plan['realtime']=first.profile_realtime.prepare(Path(old['realtime']['source_plan']),out,args.port+2,plan['sdk'])
    plan['duplex']=profile_duplex.prepare(WORK/'idle-business-01/run/plan.json',out,args.port+2,plan['sdk'])
    plan['duplex']['instruction_policy']=POLICY
    for key in ('realtime','duplex'):plan['sources'].update(plan[key]['files'])
    for n,h in plan['protected'].items():require(sha(n)==h,'default changed')
    check_plan(plan);save(out/'plan.json',plan);print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(first.Stage):
    limits=LIMITS
    request_templates=staticmethod(dict)
    success_status=SUCCESS
    requests_per_minute=30
    client_policy=first.Stage.client_policy

    def __init__(self,plan):
        check_plan(plan);encrypted.Stage.__init__(self,plan)
        self.counts.update(websocket_sessions=0,ingress_starts=0)

    def diagnostic(self,label,variant,seed):
        self.budget('chats');request=dict(copy.deepcopy(evidence.REQUESTS[label][variant]),seed=seed)
        prefix=f'primary-diagnostic-{label}-{variant}-{seed}'
        save(self.out/(prefix+'-request.json'),request)
        policy=read(self.out/'client-policy.json')
        conn=http.client.HTTPConnection('127.0.0.1',self.plan['port'],timeout=120)
        try:
            conn.request('POST','/v1/chat/completions',json.dumps(request),headers={
                'Content-Type':'application/json','X-Q4T-Client-Id':encrypted.CLIENT,
                'X-Q4T-Gateway-Token':policy['gateway_token']})
            reply=conn.getresponse();raw=reply.read((1<<20)+1)
            (self.out/(prefix+'.wire')).write_bytes(raw)
            require(reply.status==200 and len(raw)<=1<<20,'bounded diagnostic HTTP failure')
            response=json.loads(raw)
        finally:conn.close()
        self.same_model();self.drained(prefix+'-drain')
        quality=evidence.d.duplex_checks.judgment(label,response['choices'][0]['message'].get('content') or '',evidence.d.PHRASE)
        row=dict(label=label,variant=variant,seed=seed,request=request,status=reply.status,response=response,quality=quality)
        save(self.out/(prefix+'-receipt.json'),row);return row

    def profile_business(self):
        name=self.profile_name;caps=read(self.out/(name+'-capabilities.json'))
        require(profile_evidence.duplex_enabled(caps),'actual duplex capability missing')
        _,old=first.original(self.plan,name);business=copy.deepcopy(old['business'])
        if name=='primary':
            original=WORK/'profiles-01'
            require(caps==read(original/'primary-capabilities.json'),'primary capability changed')
            diagnostics=[]
            for seed in evidence.SEEDS:
                for label in ('mixed','vad_auto'):
                    for variant in ('before','after'):
                        diagnostics.append(self.diagnostic(label,variant,seed))
                        save(self.out/'primary-diagnostics.json',diagnostics)
            followup=profile_duplex.run_sessions(self,('mixed','vad_once'))
            value=dict(format=evidence.FORMAT,original=read(original/'primary-duplex-observed.json'),
                followup=followup,diagnostics=diagnostics,source_review=evidence.REVIEW)
            profile_duplex.save_observed(self.out/'primary-followup-observed.json',value)
            evidence.business(value)
            business.update(realtime_duplex=value,realtime=read(original/'primary-realtime-business.json'),
                            semantic_vad=read(original/'primary-semantic-business.json'))
        else:
            business.update(realtime=first.profile_realtime.run(self),semantic_vad=first.profile_semantic.run(self))
            business['realtime_duplex']=profile_duplex.run(self)
        profile_evidence.business(business,self.plan['identity'],self.plan['profiles'][name],caps)
        config=read(self.out/(name+'-config.json'))
        value=dict(format=profile_evidence.DUPLEX_FORMAT,profile=name,identity=self.plan['identity'],
            capacity=self.plan['profiles'][name],capabilities=caps,
            configuration=first.contracts.configuration(config,version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],business=business,
            environment=dict(status='UNQUALIFIED_PENDING_GUARD_EXIT'))
        profile_record.save(self.out/(name+'-unqualified.json'),reuse.attach(value))


def finalize(out):
    plan=read(out/'plan.json');check_plan(plan);result=read(out/'result.json')
    require(result['status']==SUCCESS and result['binary_sha256']==plan['binary_sha256'] and
        0<result['seconds']<=LIMITS['seconds'] and set(result['counts'])==set(LIMITS)-{'seconds'} and
        all(type(result['counts'][k]) is int and result['counts'][k]==LIMITS[k]
            for k in LIMITS if k not in ('seconds','controls')) and
        type(result['counts']['controls']) is int and 0<=result['counts']['controls']<=2000,
        'bounded primary repair/light scope incomplete')
    for n,h in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():require(sha(ROOT/n)==h,'postflight source drift')
    require(sha(plan['binary'])==plan['binary_sha256'],'binary drift');full.bundle.check_assets(plan['model'],plan['model_assets'])
    lease=out/'lease';queries=[json.loads(l) for l in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    cleanups=[read(out/n) for n in ('primary-cleanup.json','cleanup.json')]
    environment=full.environment(read(lease/'exit.json'),queries,cleanups)
    # This exact aggregate points to both original valid environment and follow-up.
    receipt=dict(original=evidence.REVIEW,followup=sha(lease/'exit.json'),original_command_status='COMMAND_FAILED',
                 original_quality_status='FAIL',original_protocol_accepted=True)
    save(out/'environment-composition.json',receipt)
    environment.update(lease_result_sha256=sha(out/'environment-composition.json'),nvml_queries=len(queries)+103)
    for name,kinds in [('primary',('duplex',)),('light',('realtime','semantic','duplex'))]:
        for kind in kinds:
            entry=read(out/(name+'-'+kind+'-entry-cleanup.json'))
            require(entry['exit_code']==0 and entry['port_released'] is True,'entry not cleaned')
    guard=encrypted.foreground.base.lifecycle.guard;processes=guard.processes()
    require(not any(processes.get(int(pid),{}).get('start_ticks')==row['start_ticks'] for c in
        [*cleanups,read(WORK/'profiles-01/cleanup.json')] for pid,row in c['owned'].items()),'owned process remains')
    full.background.probe_ports((plan['port'],plan['operations_port'],plan['duplex']['public_port']))
    with guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
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
        default_unchanged=True,managed_recovery_accepted=False,original_quality_status='FAIL',
        original_failures_preserved=True,caller_instruction_policy=POLICY,model_or_sampling_changed=False,
        performance_claim=False,private_asr_independent_token_oracle=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--output',type=Path,required=True);p.add_argument('--port',type=int,default=18528)
    for mode in ('run','finalize'):sub.add_parser(mode).add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    elif args.mode=='finalize':finalize(args.output.resolve())
    else:
        def interrupted(_signum,_frame):raise TimeoutError('profile follow-up deadline/interruption')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted);signal.alarm(1200)
        try:Stage(read(args.output/'plan.json')).run()
        finally:signal.alarm(0)


if __name__=='__main__':main()
