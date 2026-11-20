"""Two exact capacities: native duplex and affected standalone input owners.

One strict lease, two models, ten WSS sessions and 42 native admission units.
Existing bounded semantic scoring stays inside its owning ASR item. No full
backend/performance matrix, automatic retry, default mutation or model replay.
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
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_ops import contracts
from tools.serve_recovery import ledger,profile_acceptance as full
from tools.serve_recovery import profile_contract,profile_evidence,profile_record
from tools.serve_recovery import profile_realtime,profile_semantic,profile_duplex
from tools.serve_recovery import profile_duplex_reuse as reuse
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read,require,save,sha

FORMAT='q4t-duplex-targeted-qualification-v1'
SUCCESS='PASS_DUPLEX_PRIMARY_LIGHT'
LIMITS=dict(starts=2,chats=42,controls=2000,seconds=1200,websocket_sessions=10,ingress_starts=6)


def original(plan,name):
    path=Path(plan['previous_registry'])
    require(sha(path)==reuse.REVIEW['previous_registry_sha256'],'accepted registry drift')
    registry=read(path);source=path.parent/(name+'.json')
    require(sha(source)==reuse.SOURCES[name]['record_sha256'],'accepted record bytes drift')
    value=profile_record.read(source);profile_evidence.validate(value,registry,name,sha(source))
    return registry,value


def check_result(value):
    require(value['status']==SUCCESS and value['binary_sha256']==reuse.REVIEW['candidate_binary'] and
        0<value['seconds']<=LIMITS['seconds'] and
        set(value['counts'])==set(LIMITS)-{'seconds'} and
        all(type(value['counts'][k]) is int and value['counts'][k]==LIMITS[k]
            for k in LIMITS if k not in ('seconds','controls')) and
        type(value['counts']['controls']) is int and 0<=value['counts']['controls']<=LIMITS['controls'],
        'exact bounded duplex qualification incomplete')


def check_plan(plan):
    require(plan['targeted_format']==FORMAT and plan['limits']==LIMITS and
        plan['requests']=={} and plan['automatic_retries'] is False and
        plan['binary_sha256']==reuse.REVIEW['candidate_binary'] and
        plan['identity']['protocol_sha256']==reuse.REVIEW['candidate_protocol'] and
        plan['planned']=={k:v for k,v in LIMITS.items() if k not in ('seconds','controls')},
        'duplex frozen scope or candidate drift')
    path=Path(plan['source_review'])
    require(sha(path)==reuse.REVIEW['source_review'] and
        sha(path.with_name('runtime.diff'))==reuse.REVIEW['runtime_diff'],'reviewed source drift')
    reviewed=read(path)
    for n,h in {**reviewed['unchanged'],**reviewed['added'],
                **{n:v['after'] for n,v in reviewed['changed'].items()}}.items():
        require(sha(ROOT/n)==h,'reviewed compiled source changed: '+n)
    for name in profile_contract.NAMES:
        registry,_=original(plan,name)
        require(plan['profiles']==registry['profiles'] and
            {k:v for k,v in plan['identity'].items() if k not in ('binary_sha256','protocol_sha256')}==
            {k:v for k,v in registry['identity'].items() if k not in ('binary_sha256','protocol_sha256')},
            'backend or capacity drift')
    actor=object.__new__(Stage);actor.plan=plan
    for name in profile_contract.NAMES:
        full.capacity.check_clients(plan['profiles'][name],actor.client_policy())


def prepare(args):
    path=args.previous_registry.resolve(strict=True);previous=read(path.parent.parent/'plan.json')
    reference=read(args.profile_reference.resolve(strict=True))
    manifest=args.package.resolve(strict=True)/'engine/manifest.json';base=read(manifest)
    require(base['settings']==previous['base']['settings']==reference['base']['settings'],
            'retained backend settings drift')
    cpu=ROOT/'.q4t-work/serve-realtime-duplex-20261120/profile-duplex-cpu-02'
    require(read(cpu/'result.json')['status']=='PASS','targeted profile CPU missing')
    for n,h in read(cpu/'plan.json')['sources'].items():
        require(sha(ROOT/n)==h,'qualified profile tool drift: '+n)
    args.sdk_directory=Path(previous['sdk']['sdk_directory'])
    encrypted.foreground.base.prepare(args,rows={},limits=LIMITS,scope=__doc__)
    out=args.output.resolve();plan=read(out/'plan.json')
    base,candidate=full.candidate_build_base(base,args.binary,args.candidate_build)
    registry=read(path)
    plan.update(targeted_format=FORMAT,base=base,candidate_identity=candidate,
        previous_registry=str(path),source_review=str(args.source_review.resolve(strict=True)),
        profiles=copy.deepcopy(registry['profiles']),
        identity=dict(registry['identity'],binary_sha256=plan['binary_sha256'],protocol_sha256=base['protocol_sha256']),
        native_assets=copy.deepcopy(reference['native_assets']),chat_audio=True,semantic_vad=True,
        tts_sampling_profile=reference['tts_sampling_profile'],
        planned={k:v for k,v in LIMITS.items() if k not in ('seconds','controls')})
    plan['realtime']=profile_realtime.prepare(args.history_reference,out,plan['port']+2,plan['sdk'])
    plan['duplex']=profile_duplex.prepare(args.duplex_reference,out,plan['port']+2,plan['sdk'])
    sources=[manifest,path,path.parent.parent/'plan.json',args.profile_reference.resolve(),
        cpu/'plan.json',cpu/'result.json',
        Path(plan['source_review']),Path(plan['source_review']).with_name('runtime.diff'),
        *(path.parent/(n+'.json') for n in profile_contract.NAMES),
        *(Path(candidate[k]['path']) for k in ('compiled_sources','build_cache'))]
    names=subprocess.check_output(['git','ls-files','tools/serve_realtime','tools/serve_ops',
        'tools/serve_recovery','tools/serve_responses'],cwd=ROOT,text=True).splitlines()
    for p in [*sources,*(ROOT/n for n in names)]:plan['sources'][str(p)]=sha(p)
    for key in ('realtime','duplex'):plan['sources'].update(plan[key]['files'])
    for p in (manifest,ROOT/'build/service-native-state/ledger.json'):plan['protected'][str(p)]=sha(p)
    check_plan(plan);save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(full.Stage):
    limits=LIMITS
    request_templates=staticmethod(dict)
    success_status=SUCCESS
    requests_per_minute=30

    def __init__(self,plan):
        check_plan(plan);encrypted.Stage.__init__(self,plan)
        self.counts.update(websocket_sessions=0,ingress_starts=0)

    def client_policy(self):
        value=super().client_policy()
        value['clients']=[value['clients'][0],dict(value['clients'][0],id='audio')]
        return value

    def profile_business(self):
        name=self.profile_name;caps=read(self.out/(name+'-capabilities.json'))
        require(profile_evidence.duplex_enabled(caps),'actual native duplex capability missing')
        _,old=original(self.plan,name)
        fresh=dict(realtime=profile_realtime.run(self),semantic_vad=profile_semantic.run(self))
        fresh['realtime_duplex']=profile_duplex.run(self)
        business=copy.deepcopy(old['business']);business.update(fresh)
        profile_evidence.business(business,self.plan['identity'],self.plan['profiles'][name],caps)
        config=read(self.out/(name+'-config.json'))
        value=dict(format=profile_evidence.DUPLEX_FORMAT,profile=name,identity=self.plan['identity'],
            capacity=self.plan['profiles'][name],capabilities=caps,
            configuration=contracts.configuration(config,version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],business=business,
            environment=dict(status='UNQUALIFIED_PENDING_GUARD_EXIT'))
        # The normal validator rejects this pending environment; there is no
        # registry or accepted certificate until the external guard completes.
        profile_record.save(self.out/(name+'-unqualified.json'),reuse.attach(value))


def finalize(out):
    plan=read(out/'plan.json');check_plan(plan);result=read(out/'result.json');check_result(result)
    for n,h in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():
        require(sha(ROOT/n)==h,'postflight source drift: '+n)
    require(sha(plan['binary'])==plan['binary_sha256'],'candidate changed')
    full.bundle.check_assets(plan['model'],plan['model_assets'])
    lease=out/'lease'
    queries=[json.loads(l) for l in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    cleanups=[read(out/n) for n in ('primary-cleanup.json','cleanup.json')]
    environment=full.environment(read(lease/'exit.json'),queries,cleanups)
    environment['lease_result_sha256']=sha(lease/'exit.json')
    for name in profile_contract.NAMES:
        for kind in ('realtime','semantic','duplex'):
            entry=read(out/(name+'-'+kind+'-entry-cleanup.json'))
            require(entry['exit_code']==0 and entry['port_released'] is True,'entry not cleaned')
    guard=encrypted.foreground.base.lifecycle.guard;processes=guard.processes()
    require(not any(processes.get(int(pid),{}).get('start_ticks')==row['start_ticks']
        for c in cleanups for pid,row in c['owned'].items()),'owned process remains')
    full.background.probe_ports((plan['port'],plan['operations_port'],plan['duplex']['public_port']))
    with guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    qualified=out/'qualified';qualified.mkdir(mode=0o700)
    registry=dict(format=profile_contract.FORMAT,identity=plan['identity'],profiles=plan['profiles'],qualifications={})
    for name in profile_contract.NAMES:
        value=profile_record.read(out/(name+'-unqualified.json'))
        require(value['environment']==dict(status='UNQUALIFIED_PENDING_GUARD_EXIT'),
                'pending record provenance differs')
        value['environment']=environment;path=qualified/(name+'.json');profile_record.save(path,value)
        registry['qualifications'][name]=profile_contract.qualification(name,value['capacity'],
            value['identity'],value['capabilities'],value['configuration'],value['startup_memory'],sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path=qualified/(name+'.json');profile_evidence.validate(profile_record.read(path),registry,name,sha(path))
    save(qualified/'registry.json',registry)
    save(out/'acceptance.json',dict(status='ACCEPT_DUPLEX_PRIMARY_LIGHT',environment=environment,
        counts=result['counts'],seconds=result['seconds'],registry_sha256=sha(qualified/'registry.json'),
        default_unchanged=True,managed_recovery_accepted=False,performance_claim=False,
        original_failures_preserved=True,private_asr_independent_token_oracle=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare')
    for key in ('binary','package','candidate-build','previous-registry','profile-reference',
                'source-review','history-reference','duplex-reference','output'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--port',type=int,default=18518)
    for mode in ('run','finalize'):sub.add_parser(mode).add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    elif args.mode=='finalize':finalize(args.output.resolve())
    else:
        def interrupted(_signum,_frame):raise TimeoutError('duplex profile deadline/interruption')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted)
        signal.alarm(LIMITS['seconds'])
        try:Stage(read(args.output/'plan.json')).run()
        finally:signal.alarm(0)


if __name__=='__main__':main()
