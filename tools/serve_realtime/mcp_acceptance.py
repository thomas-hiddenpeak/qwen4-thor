"""Bounded public Realtime MCP on both actual managed capacities.

Two starts, eight model jobs, two WSS/TLS sessions, 900 seconds, one GPU lease.
Actual project reads, approval/denial and lost receipt; no retry, numerical
change, full matrix, default mutation or implicit model/tool replay.
"""
import argparse
import copy
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import duplex_acceptance, mcp_checks, mcp_lifecycle
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_acceptance as full, profile_realtime
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_service.package import read, require, save, sha

WORK = ROOT/'.q4t-work/serve-realtime-mcp-20261120'
REFERENCE = ROOT/'.q4t-work/serve-realtime-duplex-20261120/cancel-usage-business-01/plan.json'
LIMITS = dict(starts=2, chats=8, controls=1200, seconds=900,
              websocket_sessions=2, ingress_starts=2)
FORMAT = 'q4t-realtime-mcp-business-v1'
FOLLOWUP_FORMAT = 'q4t-realtime-mcp-business-followup-v1'
FOLLOWUP_LIMITS = dict(LIMITS, chats=5)
FINISH_FORMAT = 'q4t-realtime-mcp-business-unknown-v1'
FINISH_LIMITS = dict(LIMITS, starts=1, chats=2, websocket_sessions=1, ingress_starts=1)
SUCCESS = 'PASS_REALTIME_MCP_PRIMARY_LIGHT'


def check_plan(plan):
    limits = {FORMAT: LIMITS, FOLLOWUP_FORMAT: FOLLOWUP_LIMITS,
              FINISH_FORMAT: FINISH_LIMITS}.get(plan['format'])
    require(limits is not None and plan['limits'] == limits and
            plan['requests'] == {} and plan['automatic_retries'] is False and
            plan['planned'] == {k:v for k,v in limits.items() if k not in ('seconds','controls')},
            'frozen MCP scope differs')
    built = read(WORK/'session-runtime-01/result.json')
    require(built['status'] == 'BUILD_PASS' and built['warnings'] == 0 and
            plan['binary_sha256'] == built['binary_sha256'], 'runtime build identity')
    previous = read(REFERENCE)
    for name in ('profiles', 'native_assets', 'model_assets'):
        require(plan[name] == previous[name], 'backend/capacity changed: '+name)
    require(plan['base']['settings'] == previous['base']['settings'], 'engine settings drift')
    for name, digest in plan['reviewed_sources'].items():
        require(sha(ROOT/name) == digest, 'compiled source drift: '+name)


def prepare(out, followup=False, finish=False):
    require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
            'commit source and targeted verifier checks before GPU')
    cpu_dir = WORK/'sdk-cpu-03'
    cpu = read(cpu_dir/'result.json')
    require(cpu['status'] == 'PASS', 'targeted verifier checks missing')
    for n,h in cpu['sources'].items():require(sha(ROOT/n)==h,'verifier CPU source drift')
    accepted = read(ROOT/'docs/evidence/serve-realtime-mcp-session-cpu-20261120/acceptance.json')
    require(accepted['distinct_cases'] == 38 and accepted['production_sources_identical_across_cpu_runs'],
            'public MCP source CPU evidence incomplete')
    out.mkdir(mode=0o700, parents=True)
    plan = read(REFERENCE)
    build = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
    base, binding = full.candidate_build_base(plan['base'], build/'q4t', build)
    current = read(WORK/'session-runtime-01/plan.json')['sources']
    current.update(read(build/'generated/q4t/server/chat_protocol_sources.json'))
    for key in ('duplex', 'realtime', 'conversation', 'prompt', 'repair_source_review', 'targeted_format'):
        plan.pop(key, None)
    limits = FINISH_LIMITS if finish else FOLLOWUP_LIMITS if followup else LIMITS
    plan.update(format=FINISH_FORMAT if finish else FOLLOWUP_FORMAT if followup else FORMAT,
        limits=limits, output=str(out), port=18618,
        operations_port=18619, public_port=18620, private_peer_port=18621,
        scope=__doc__, binary=str(build/'q4t'), binary_sha256=sha(build/'q4t'),
        base=base, candidate_identity=binding, reviewed_sources=current,
        identity=dict(plan['identity'], binary_sha256=sha(build/'q4t'),
                      protocol_sha256=base['protocol_sha256']),
        planned={k:v for k,v in limits.items() if k not in ('seconds','controls')},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        ingress=str(ROOT/'.q4t-work/serve-file-search-20261112/host/q4t-ingress'),
        tls=certificate(out/'credentials'))
    record, _ = peer.DocumentSource(ROOT).read()
    plan['document_source'] = record['source']
    paths = subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/serve_realtime','tools/serve_recovery','tools/serve_service','tools/serve_responses',
        'tools/serve_grammar_reap','tools/serve_schema_v2','tools/serve_protocol','tools/examples',
        'tools/evalscope','tools/release','docs/MODEL.md'],cwd=ROOT,text=True).splitlines()
    plan['sources'] = {n:sha(ROOT/n) for n in paths}
    for p in [REFERENCE,Path(plan['ingress']),Path(plan['tls']['certificate']),
              Path(plan['tls']['key']),cpu_dir/'result.json',
              WORK/'session-runtime-01/result.json',WORK/'session-runtime-01/plan.json']:
        plan['sources'][str(p)] = sha(p)
    if finish:
        require(not followup, 'one explicit followup mode')
        plan['scope'] = ('Only the previously unexecuted lost-receipt and ordinary jobs; '
                         'fresh explicit user task; original quality failures preserved')
        for stage in ('sdk-business-02', 'sdk-business-03'):
            for p in (WORK/stage).glob('*.sdk.json'):
                plan['sources'][str(p)] = sha(p)
    if followup:
        previous = WORK/'sdk-business-02'
        require(read(previous/'result.json')['counts']['starts']==1 and
                read(previous/'cleanup.json')['model_exit']==0 and
                read(previous/'primary-mcp.sdk.json')['quality'][0]['status']=='PASS',
                'exact retained primary result required')
        receipt = previous/'primary-mcp.sdk.json'
        plan['retained_receipt'] = str(receipt)
        plan['sources'][str(receipt)] = sha(receipt)
        original = previous/'application-objects'
        files = [p for p in original.rglob('*') if p.is_file()]
        require(not any(p.is_symlink() for p in original.rglob('*')) and
                sum(p.stat().st_size for p in files)<=32<<20, 'bounded owned state copy')
        for p in files:plan['sources'][str(p)] = sha(p)
        shutil.copytree(original,out/'application-objects')
    plan['protected'] = {str(ROOT/n):sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json',
        'build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json')}
    check_plan(plan); save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=limits)),flush=True)


class Stage(full.Stage):
    limits = LIMITS
    request_templates = staticmethod(dict)
    success_status = SUCCESS
    requests_per_minute = 30

    def __init__(self, plan):
        check_plan(plan); self.limits=plan['limits']; encrypted.Stage.__init__(self, plan)
        self.counts.update(websocket_sessions=0, ingress_starts=0)
        self.peer = self.edge = None
        self.observed = {}

    def start(self, selected=None):
        name = selected or ('light' if self.plan['format'] == FINISH_FORMAT else
                            'primary' if self.counts['starts'] == 0 else 'light')
        self.profile_name = name
        full.background.probe_ports(tuple(self.plan[k] for k in
            ('port','operations_port','public_port','private_peer_port')))
        for path,digest in self.plan['native_assets'].items():
            require(sha(path)==digest,'qualified native asset drift')
        stage = self
        class Source(peer.DocumentSource):
            def read(self):
                value = super().read()
                # The second tools/call on light really reads the document,
                # then loses its receipt. A resource read may arm the same
                # single future drop; it never executes or retries a tool.
                if name == 'light' and len(stage.peer.calls) == 1:
                    stage.peer.drop_next = True
                return value
        (self.out/(name+'-peer')).mkdir(mode=0o700)
        self.peer = peer.ProjectMcp(ROOT, self.out/(name+'-peer'),
                                    self.plan['private_peer_port'], source=Source(ROOT))
        if self.plan['format'] == FINISH_FORMAT:
            self.peer.drop_next = True
        save(self.out/'mcp-private-policy.json',dict(version=1,fetch=self.peer.fetch(),targets=[]))
        policy = self.client_policy(); full.capacity.check_clients(self.plan['profiles'][name],policy)
        save(self.out/'client-policy.json',policy)
        args = full.command(self.plan,name,self.out)
        args += ['--mcp-policy-file',str(self.out/'mcp-private-policy.json')]
        save(self.out/(name+'-command.json'),args); self.budget('starts')
        with (self.out/(name+'-model.log')).open('xb',buffering=0) as log:
            self.model = subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                          pass_fds=(self.lease,))
        guard = encrypted.foreground.base.lifecycle.guard
        self.model_identity = guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/(name+'-identity.json'),dict(pid=self.model.pid,**self.model_identity))
        end = time.monotonic()+180
        while time.monotonic()<end:
            self.same_model()
            try:
                if self.http('/readyz',timeout=1)[0] == 200: break
            except (OSError,http.client.HTTPException): pass
            time.sleep(.1)
        else: raise TimeoutError('MCP actual model readiness')
        for key in ('config','capabilities'):
            status,raw = self.http('/v1/'+key,label=name+'-'+key)
            require(status==200,'actual '+key+' missing'); save(self.out/(name+'-'+key+'.json'),json.loads(raw))
        config = read(self.out/(name+'-config.json'))
        full.profile_contract.configuration(self.plan['profiles'][name],
                                            full.contracts.configuration(config,version=5))
        full.profile_contract.startup_memory(config['startup_memory']['checks']['before_allocations'])

    def profile_business(self):
        name = self.profile_name; label=name+'-mcp'; plan=self.plan
        policy=read(self.out/'client-policy.json'); key=hashlib.sha256(os.urandom(32)).hexdigest()
        self.budget('ingress_starts')
        self.edge=NativeIngress(self.out/label,plan['ingress'],plan['public_port'],plan['port'],
            plan['operations_port'],policy['gateway_token'],{encrypted.CLIENT:key},plan['tls'],
            lambda:self.model.poll() is None)
        guard=encrypted.foreground.base.lifecycle.guard
        self.history[self.edge.process.pid]=guard.processes()[self.edge.process.pid]
        def observe(suffix):
            status,raw=self.http('/metrics',label=label+'-'+suffix)
            require(status==200,'MCP actual metrics missing')
            return profile_realtime.state(encrypted.foreground.base.lifecycle.parse_metrics(raw),encrypted.CLIENT)
        before=observe('before'); self.budget('websocket_sessions')
        repair = plan['format']==FOLLOWUP_FORMAT and name=='primary'
        finish = plan['format'] == FINISH_FORMAT
        for _ in range(2 if finish else 1 if repair else 4): self.budget('chats')
        source=plan['document_source']
        args=dict(mode='unknown-only' if finish else 'ordinary-repair' if repair else
            mcp_checks.MODES[0 if name=='primary' else 1],
            base_url=f"https://127.0.0.1:{plan['public_port']}/v1",api_key=key,
            ca_file=plan['tls']['certificate'],mcp_url=self.peer.url,mcp_token=self.peer.secret,
            arguments=peer.ARGUMENTS,title=source['text'].splitlines()[0].split(': ',1)[1],
            citation=source['citation'],journal_file=str(self.out/(label+'.events.jsonl')))
        if repair:args['retained_history']=read(Path(plan['retained_receipt']))['tools']
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR']=plan['sdk']['sdk_directory']
        receipt=self.out/(label+'.sdk.json')
        process=duplex_acceptance.capture_actor([plan['sdk']['node']['path'],
            str(ROOT/'tools/serve_realtime/mcp_sdk.mjs')],args,receipt,self.out/(label+'.stderr'),env,330)
        require(receipt.stat().st_size<=8<<20,'SDK receipt storage bound')
        observed=read(receipt)
        self.same_model(); self.drained(label+'-drain')
        after=observe('after-0'); end=time.monotonic()+5; index=0
        while (after['sessions'] or after['host_bytes']) and time.monotonic()<end:
            time.sleep(.02); index+=1; after=observe('after-'+str(index))
        journal=Path(args['journal_file']);require(journal.stat().st_size<=8<<20,'journal bound')
        remote=self.peer.snapshot()
        value=dict(session=observed,remote=remote,before=before,after=after)
        save(self.out/(label+'-observed.json'),value)
        require(process.returncode in (0,1) and observed['status'] in ('PASS','QUALITY_FAIL'),
                'actual SDK protocol failure; keep original journal and settlement')
        original_events=[json.loads(l) for l in journal.read_text().splitlines()]
        self.observed[name]=(mcp_checks.observe_unknown(observed,original_events,remote,
            source,before,after) if finish else mcp_checks.observe_repair(observed,original_events,remote,
            args['retained_history'],before,after) if repair else
            mcp_checks.observe(observed,original_events,remote,source,before,after))
        save(self.out/'observations.json',self.observed)
        for p in [receipt,journal,self.out/(name+'-model.log'),
                  *(p for p in (self.out/'application-objects').rglob('*') if p.is_file())]:
            require(self.peer.secret.encode() not in p.read_bytes(),'MCP credential leaked')
        self.edge.close()
        save(self.out/(name+'-entry-cleanup.json'),dict(exit_code=self.edge.process.returncode,
             pid=self.edge.process.pid,port_released=True))
        self.edge=None

    def business(self):
        if self.plan['format'] == FINISH_FORMAT:
            self.profile_business()
        else:
            full.Stage.business(self)
        require(all(q['status']=='PASS' for r in self.observed.values() for q in r['quality']),
                'model quality failure retained separately from protocol observations')

    def cleanup(self):
        try:
            if self.edge is not None:self.edge.close();self.edge=None
            super().cleanup()
        finally:
            if self.peer is not None:
                self.peer.close();save(self.out/(self.profile_name+'-peer-results.json'),self.peer.snapshot())
                self.peer=None


def finalize(out):
    plan=read(out/'plan.json'); check_plan(plan); result=read(out/'result.json')
    require(result['status']==SUCCESS and 0<result['seconds']<=900 and
            all(result['counts'][k]==v for k,v in plan['limits'].items() if k not in ('seconds','controls')),
            'bounded MCP business incomplete')
    for n,h in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():
        require(sha(ROOT/n)==h,'postflight source/default drift')
    finish = plan['format'] == FINISH_FORMAT
    lease=out/'lease'
    clean=[read(out/n) for n in (('cleanup.json',) if finish else
                               ('primary-cleanup.json','cleanup.json'))]
    environment=full.environment(read(lease/'exit.json'),
        [json.loads(l) for l in (lease/'nvml-queries.jsonl').read_text().splitlines()],
        clean, expected_starts=plan['limits']['starts'])
    lifecycle = {}
    for name in (('light',) if finish else ('primary','light')):
        require(read(out/(name+'-entry-cleanup.json'))['exit_code']==0, 'entry cleanup')
        lifecycle[name] = mcp_lifecycle.retained(
            read(out/(name+'-mcp.sdk.json'))['events'],
            read(out/(name+'-peer-results.json')), out/'application-objects',
            'client:' + encrypted.CLIENT)
    full.background.probe_ports(tuple(plan[k] for k in ('port','operations_port','public_port','private_peer_port')))
    guard=encrypted.foreground.base.lifecycle.guard
    with guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    save(out/'acceptance.json',dict(status=SUCCESS,environment=environment,counts=result['counts'],
        seconds=result['seconds'],default_unchanged=True,managed_recovery=False,
        qualification_issued=False,observations=read(out/'observations.json'),
        mcp_lifecycle=lifecycle))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('prepare','run','finalize'))
    parser.add_argument('--followup',action='store_true')
    parser.add_argument('--finish',action='store_true')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output.resolve()
    require(out.is_relative_to(WORK),'private MCP stage directory')
    if args.mode=='prepare':prepare(out,args.followup,args.finish)
    elif args.mode=='finalize':finalize(out)
    else:
        def interrupted(_signum,_frame):raise TimeoutError('bounded MCP stage interrupted')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted);signal.alarm(900)
        try:
            with encrypted.foreground.base.lifecycle.child_subreaper():Stage(read(out/'plan.json')).run()
        finally:signal.alarm(0)
