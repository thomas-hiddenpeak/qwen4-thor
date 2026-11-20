"""Native MCP approval/history survives gateway/controller/whole-payload recovery.

Every model control action belongs to the sealed production Guardian. The
acceptance actor only signals exact owned fault targets and explicitly starts
new client connections; interrupted work is never replayed.
"""
import copy
import json
from pathlib import Path
import runpy
import subprocess
import sys
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
P=runpy.run_path(str(ROOT/'tools/serve_realtime/prompt_service_acceptance.py'))
BASE,GLOBALS=P['BASE'],P['GLOBALS']
VERSION,MANAGED,LEGACY=P['VERSION'],P['MANAGED'],P['LEGACY']
require,save=P['require'],P['save']

# Counts include exactly one startup selfcheck per model. Configured private
# MCP routing is separate from the unchanged production default network policy.
CONFIGURED_LIMITS = dict(
    tool_retained=dict(starts=1,chats=3,probes=0,controls=1500,websocket_sessions=1,seconds=240),
    tls=dict(starts=2,chats=7,probes=0,controls=2000,websocket_sessions=3,seconds=360),
    tool_version=dict(starts=2,chats=6,probes=1,controls=2500,websocket_sessions=2,seconds=420))
DEFAULT_LIMITS = dict(
    tool_retained=dict(starts=1,chats=2,probes=0,controls=1500,websocket_sessions=1,seconds=180),
    tool_version=dict(starts=2,chats=4,probes=1,controls=2000,websocket_sessions=2,seconds=300))
LIMITS = CONFIGURED_LIMITS


def configure(default):
    global LIMITS
    require(type(default) is bool, 'explicit default policy selection')
    LIMITS = DEFAULT_LIMITS if default else CONFIGURED_LIMITS
    GLOBALS['LIMITS'] = LIMITS


def prepare(args):
    bundle,_,_,_,_,engine=LEGACY['load_modules']()
    from tools.serve_realtime import mcp_managed as mcp
    from tools.serve_recovery import profile_evidence,profile_record
    from tools.serve_realtime import managed
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            'commit reviewed source before freezing')
    out=args.output.resolve();require(out.is_relative_to(ROOT/'.q4t-work'),'private recovery output required')
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    pack=args.package.resolve(strict=True);digest=engine.sha(pack/'manifest.json');manifest=bundle.verify(pack,digest)
    policy=engine.read(pack/'gateway-policy.json')
    require(manifest['ingress_mode']=='native' and bool(manifest['tls'])==args.scenario.startswith('tls') and
        policy['listen_host']=='127.0.0.1','exact native local/TLS payload required')
    qualified=profile_record.read(pack/'profile-primary.json')
    require(profile_evidence.mcp_enabled(qualified['capabilities']),'actual qualified MCP required')
    fixture=engine.read(args.fixture.resolve(strict=True))
    default=not (pack/'engine/mcp-policy.json').exists()
    configure(default)
    require(args.scenario in LIMITS,'unplanned default/TLS scenario')
    from tools.serve_responses.mcp_business_peer import DocumentSource
    record,_=DocumentSource(ROOT).read()
    require(fixture['source']==record['source'],'actual project input changed')
    require(default or engine.read(pack/'engine/mcp-policy.json')==fixture['policy'],
            'configured package must seal the exact explicit fixture policy')
    source=args.profile_reference.resolve(strict=True)
    prior=managed.accepted_source(source,manifest,qualified,pack)
    sdk=prior['sdk']
    names=subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_host',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope','tools/examples',
        'tools/release','tools/serve_responses','tools/serve_realtime','src/service','include/q4t/service'],
        cwd=ROOT,text=True).splitlines()
    plan=dict(version=1,scenario=args.scenario,limits=LIMITS[args.scenario],
        package=dict(package=str(pack),manifest_sha256=digest),output=str(out),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n),sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json',
             'build/service.release.json','build/service-native-state/ledger.json')],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        command=[sys.executable,'-I','-B',str(Path(__file__).resolve()),'run','--plan',str(out/'plan.json')],
        requests={},sdk=copy.deepcopy(sdk),mcp_recovery=dict(format=mcp.FORMAT,
            registry=manifest['recovery_profiles'],profile_reference=str(source),
            default_policy=default,fixture=fixture,source=record['source']),
        default_replacement=False,automatic_request_replay=False,scope=__doc__)
    for path in (source,source.parent/'acceptance.json',source.parent/'qualified/registry.json',
                 source.parent/'qualified/primary.json',source.parent/'qualified/light.json'):
        plan['sources'][str(path)]=engine.sha(path)
    for path in (args.fixture.resolve(strict=True), ROOT/'docs/MODEL.md',
                 *(Path(v) for v in fixture['tls'].values())):
        plan['sources'][str(path)]=engine.sha(path)
    plan['sources'].update({str(Path(sdk['sdk_directory'])/n):h for n,h in sdk['files'].items()})
    plan['sources'][sdk['node']['path']]=sdk['node']['sha256']
    del plan['sdk']['files'];plan['sdk']['source_inventory_count']=len(sdk['files'])
    if args.scenario.startswith('tls'):
        plan['tls_inputs']={}
        for key in ('certificate','key'):
            path=Path(manifest['tls_configuration'][key]).resolve(strict=True)
            require(path.is_relative_to(out.parent),'private TLS material required')
            plan['tls_inputs'][key]=dict(path=str(path),sha256=engine.sha(path))
        plan['tls_inputs']['ca']=plan['tls_inputs']['certificate']
    if args.scenario=='tool_version':
        from tools.serve_recovery import version_contract
        require(args.retained and args.release and args.decision,'retained MCP acceptance must finalize first')
        retained=args.retained.resolve(strict=True)
        accepted={n:dict(path=str(p.resolve(strict=True)),sha256=engine.sha(p))
                  for n,p in [('release',args.release),('decision',args.decision)]}
        plan['registration']=version_contract.prepare(plan['package'],plan['package'],
            dict(package=str(retained),manifest_sha256=engine.sha(retained/'manifest.json')),
            str(uuid.uuid4()),fallback_acceptance=accepted)
        helper=out/'counted-cuda-probe'
        helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
            str(Path(__file__).resolve()),'probe','--plan',str(out/'plan.json')])+
            '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        helper.chmod(0o500);plan['probe_wrapper']=dict(path=str(helper),sha256=engine.sha(helper))
    save(out/'plan.json',plan)
    require(engine.read(out/'plan.json')==plan,'bounded plan did not round trip')
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=plan['limits'])))


class Stage(P['Stage']):
    def run(self):
        from tools.serve_responses.mcp_business_peer import ProjectMcp
        fixture=self.plan['mcp_recovery']['fixture']
        self.peer=ProjectMcp(ROOT,self.output/'peer',fixture['port'],tls=fixture['tls'])
        try:return super().run()
        finally:
            self.peer.close()
            save(self.output/'mcp-peer-final.json',self.peer.snapshot())
            save(self.output/'mcp-peer-cleanup.json',dict(closed=True,port=fixture['port']))

    def exercise(self):
        from tools.serve_realtime import mcp_managed as mcp
        version=self.plan['scenario']=='tool_version';tls=self.plan['scenario']=='tls'
        first=self.version_ready(1,'primary') if version else self.ready(1,'primary')
        self.request('capabilities',path='/v1/capabilities')
        actor=mcp.Actor(self)
        try:
            actor.start('tls-before' if tls else 'primary')
            if tls:
                self.kill(first,'gateway','mcp-gateway-exit');actor.finish()
                second=self.wait(lambda s:s['business_ready'] and
                    s['gateway_generation']==first['gateway_generation']+1)
                require(all(second[k]==first[k] for k in ('guardian_pid','model_pid',
                    'model_start_ticks','model_generation','controller_pid','controller_start_ticks')),
                    'gateway recovery changed healthy model')
                actor.start('tls-after')
                self.kill(second,'controller','mcp-controller-exit')
                third=self.controller_ready()
                require(third['controller_generation']==second['controller_generation']+1 and
                    third['model_pid']!=second['model_pid'] and all(third[k]==second[k] for k in
                    ('guardian_pid','gateway_pid','gateway_start_ticks')),'controller did not recover its owned model once')
                actor.start('tls-controller')
                save(self.output/'gateway-continuity.json',dict(before=first,after=second))
                save(self.output/'controller-continuity.json',dict(before=second,after=third))
            elif version:
                before=self.family('profiles');save(self.output/'profiles-before.json',before)
                self.fatal_gateway(first);actor.finish();second=self.version_ready(2,'fallback')
                require(first['guardian_pid']==second['guardian_pid'] and all(
                    first[r+'_pid']!=second[r+'_pid'] and first[r+'_generation']<second[r+'_generation']
                    for r in ('controller','model','gateway')),'whole payload not replaced')
                require(second['version_recovery']['probe_count']==1 and
                    second['version_recovery']['last_fault']['signal']==6,'actual device/fault probe differs')
                actor.start('fallback')
                after=self.family('profiles');save(self.output/'profiles-after.json',after)
                require(all(before[k]==after[k] for k in
                    ('history','pending','selected','not_before_ns','light_since_ns')),'fallback changed profile history')
                save(self.output/'version-continuity.json',dict(before=first,after=second))
            actor.finish_phases()
        finally:actor.close()
        self.request('capabilities-after',path='/v1/capabilities')
        save(self.output/'mcp-peer-final.json',self.peer.snapshot())
        save(self.output/'media-usage.json',check_business(self.output))
        save(self.output/'business-final.json',business_record(version))


def check_business(out):
    from tools.serve_realtime import mcp_managed as mcp
    from tools.serve_service import package
    from tools.serve_recovery import profile_evidence
    out=Path(out);plan=package.read(out.parent/'plan.json')
    caps=json.loads((out/'capabilities.wire').read_bytes())
    require(caps==json.loads((out/'capabilities-after.wire').read_bytes()) and
        profile_evidence.mcp_enabled(caps),'MCP capability lost on recovery')
    rows=mcp.check(out,plan)
    if plan['scenario']=='tls':
        row=package.read(out/'controller-usage.json');v=row['verification']
        require(P['cold_controller_usage'](row['usage'],2,v['observed_deadline'],v['checked_at'])==v,
                'actual full-window controller quota receipt differs')
    return dict(mcp=rows,automatic_replay=False,
        configured_private_policy=not plan['mcp_recovery']['default_policy'],
        actual_reads=sum(r['actual_reads'] for r in rows.values()),
        authenticated_history=sum(r['history_authenticated'] for r in rows.values()),
        model_quality_separate=True,model_quality_status='PASS' if all(
            r['model_quality_status']=='PASS' for r in rows.values()) else 'FAIL')


def business_record(version):
    return dict(status='PASS',native_realtime_mcp=True,explicit_policy_scope=True,
        normal_close_and_settlement=True,version_switch=version,default_replaced=False,
        automatic_request_or_tool_replay=False,model_quality_separate=True,
        acceptance_scope='MCP network policy, approval, real reads, history, recovery ownership and settlement')


def finalize(path):
    import socket
    plan=json.loads(Path(path).read_bytes())
    # Production imports must follow namespace pinning, including metadata
    # readers. Otherwise the checkout shadows the sealed recovery package.
    _,_,_,_,_,engine=LEGACY['load_modules'](plan)
    row=engine.read(Path(plan['output'])/'execution/mcp-peer-cleanup.json')
    require(row==dict(closed=True,port=plan['mcp_recovery']['fixture']['port']),
            'owned MCP peer cleanup missing')
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind(('127.0.0.1',row['port']))
    return BASE['finalize'](path)


GLOBALS.update(__file__=__file__,LIMITS=LIMITS,prepare=prepare,Stage=Stage,
    check_business=check_business,business_record=business_record,finalize=finalize,
    PREPARE_PATH_ARGUMENTS=('profile-reference','fixture'),ACCEPTANCE_PREFIX='REALTIME_MCP_SERVICE_')
for function in (VERSION['launcher'],MANAGED['launcher']):function.__globals__['__file__']=__file__
if __name__=='__main__':
    if '--plan' in sys.argv:
        plan=json.loads(Path(sys.argv[sys.argv.index('--plan')+1]).read_text())
        configure(plan['mcp_recovery']['default_policy'])
    BASE['main']()
