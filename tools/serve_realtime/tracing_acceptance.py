"""Two actual capacities, six jobs, one strict GPU lease, at most 900 seconds.

Native TLS/official SDK, real project MCP, scoped durable traces, unavailable
storage and killed auxiliary worker. Never restart a healthy model or replay
a tool. Model-quality failures remain distinct from protocol acceptance.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import mcp_acceptance as previous, tracing_checks
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_acceptance as full, profile_realtime
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_service.package import read, require, save, sha

WORK = ROOT/'.q4t-work/serve-realtime-tracing-20261120'
REFERENCE = ROOT/'.q4t-work/serve-realtime-mcp-20261120/binding-business-02/plan.json'
LIMITS = dict(starts=2, chats=6, controls=1200, seconds=900,
              websocket_sessions=2, ingress_starts=2)
FORMAT = 'q4t-realtime-tracing-business-v1'
SUCCESS = 'PASS_REALTIME_TRACING_PRIMARY_LIGHT_PROTOCOL'
STORAGE_MARKER = b'q4t-owned-trace-unavailable-fixture-v1\n'
# Retain the already qualified secondary principal; do not grow the fair queue.
OTHER = 'voice-other'
guard = encrypted.foreground.base.lifecycle.guard


def repair_storage(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and
            path.read_bytes() == STORAGE_MARKER, 'only owned storage fault may be repaired')
    path.unlink(); path.mkdir(mode=0o700)


def trace_worker(records, model_pid, model_identity):
    require(records.get(model_pid, {}).get('start_ticks') == model_identity['start_ticks'],
            'original model identity lost')
    selected = [(pid, row) for pid,row in records.items() if
        row.get('ppid') == model_pid and row.get('argv', [])[:1] == ['q4t-traces'] and
        row.get('start_ticks', 0) >= model_identity['start_ticks']]
    require(len(selected) == 1, 'one directly owned native trace worker required')
    return selected[0]


def check_plan(plan):
    require(plan['format'] == FORMAT and plan['limits'] == LIMITS and
        plan['requests'] == {} and plan['automatic_retries'] is False and
        plan['planned'] == {k:v for k,v in LIMITS.items() if k not in ('seconds','controls')},
        'frozen tracing scope differs')
    built = read(WORK/'native-runtime-04/result.json')
    require(built['status'] == 'BUILD_PASS' and
        all(r['warnings'] == r['returncode'] == 0 for r in built['commands']) and
        plan['binary_sha256'] == built['binary_sha256'] and
        sha(plan['ingress']) == built['ingress_sha256'], 'native build identity')
    old = read(REFERENCE)
    require(all(plan[k] == old[k] for k in ('profiles','native_assets','model_assets')) and
        plan['base']['settings'] == old['base']['settings'], 'backend/capacity/settings drift')
    for n,h in plan['reviewed_sources'].items():require(sha(ROOT/n)==h,'built source drift: '+n)
    probe=object.__new__(Stage);probe.plan=plan
    policy=probe.client_policy()
    for profile in plan['profiles'].values():full.capacity.check_clients(profile,policy)


def prepare(out):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            'commit reviewed implementation and verifier before GPU')
    cpu = read(WORK/'sdk-cpu-04/result.json')
    require(cpu['status']=='PASS','targeted driver/checker CPU missing')
    for n,h in cpu['sources'].items():require(sha(ROOT/n)==h,'verified source drift')
    require(all(read(WORK/n/'result.json')['status']=='PASS' for n in
        ('native-integration-cpu-01','session-identity-cpu-02')),
        'native integration and session identity CPU missing')
    out.mkdir(mode=0o700,parents=True)
    plan=read(REFERENCE);build=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
    base,binding=full.candidate_build_base(plan['base'],build/'q4t',build)
    reviewed=read(WORK/'native-runtime-04/plan.json')['sources']
    reviewed.update(read(build/'generated/q4t/server/chat_protocol_sources.json'))
    plan.update(format=FORMAT,limits=LIMITS,output=str(out),port=18638,
        operations_port=18639,public_port=18640,private_peer_port=18641,
        scope=__doc__,prior_attempts=dict(starts=1,actual_generations=0,
            failed_record=str(WORK/'sdk-business-04/result.json'),
            reason='Fix missing native session identity; actual business did not start',
            cumulative_model_start_limit=3,cumulative_actual_generation_limit=6),
        binary=str(build/'q4t'),binary_sha256=sha(build/'q4t'),
        base=base,candidate_identity=binding,reviewed_sources=reviewed,
        identity=dict(plan['identity'],binary_sha256=sha(build/'q4t'),protocol_sha256=base['protocol_sha256']),
        planned={k:v for k,v in LIMITS.items() if k not in ('seconds','controls')},
        ingress=str(ROOT/'.q4t-work/serve-file-search-20261112/host/q4t-ingress'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        tls=certificate(out/'credentials'))
    record,_=peer.DocumentSource(ROOT).read();plan['document_source']=record['source']
    paths=subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/serve_realtime','tools/serve_recovery','tools/serve_service','tools/serve_responses',
        'tools/serve_grammar_reap','tools/serve_schema_v2','tools/serve_protocol','tools/examples',
        'tools/evalscope','tools/release','docs/MODEL.md'],cwd=ROOT,text=True).splitlines()
    plan['sources']={n:sha(ROOT/n) for n in paths}
    for p in [REFERENCE,Path(plan['ingress']),Path(plan['tls']['certificate']),
        Path(plan['tls']['key']),WORK/'native-runtime-04/result.json',
        WORK/'native-runtime-04/plan.json',WORK/'native-integration-cpu-01/result.json',
        WORK/'sdk-cpu-04/result.json',WORK/'session-identity-cpu-02/result.json']:
        plan['sources'][str(p)]=sha(p)
    plan['protected']={str(ROOT/n):sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json',
        'build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json')}
    check_plan(plan);save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)),flush=True)


class Stage(previous.Stage):
    limits=LIMITS
    request_templates=staticmethod(dict)
    success_status=SUCCESS

    def __init__(self,plan):
        check_plan(plan)
        # Reuse ownership/cleanup, not the old MCP stage's frozen scope.
        encrypted.Stage.__init__(self,plan)
        self.counts.update(websocket_sessions=0,ingress_starts=0)
        self.peer=self.edge=None
        self.observed={}
        self.actor=None;self.faults=[]
        self.trace_directory=self.out/'application-objects-traces'

    def client_policy(self):
        policy=super().client_policy()
        require(any(c['id']==OTHER for c in policy['clients']) and OTHER!=encrypted.CLIENT,
                'qualified independent observation client missing')
        return policy

    def start(self,selected=None):
        if self.counts['starts']==0:
            with self.trace_directory.open('xb') as stream:stream.write(STORAGE_MARKER)
        super().start(selected)
        require(read(self.out/(self.profile_name+'-command.json'))[-1] ==
            str(self.out/'mcp-private-policy.json'),'owned MCP policy missing')
        args=read(self.out/(self.profile_name+'-command.json'))
        require(args[args.index('--object-store-dir')+1]==str(self.out/'application-objects'),
                'trace directory must be derived from the actual shared state directory')

    def fault(self,name,control):
        require(name == ('repair-storage' if self.profile_name=='primary' else 'kill-trace-worker'),
                'unexpected profile fault')
        self.same_model();status,raw=self.http('/v1/config',label=self.profile_name+'-fault-before')
        require(status==200,'before fault health missing')
        before=json.loads(raw)['realtime_trace_health']
        row=dict(name=name,model_before=dict(pid=self.model.pid,**self.model_identity),before=before)
        if name=='repair-storage':
            require(before['failures']>0,'no observed storage failure')
            repair_storage(self.trace_directory)
        else:
            pid,identity=trace_worker(guard.processes(),self.model.pid,self.model_identity)
            self.history[pid]=identity;row['worker']=dict(pid=pid,**identity)
            guard.signal_identity(pid,identity,signal.SIGKILL)
        self.same_model();ready,_=self.http('/readyz',label=self.profile_name+'-fault-ready')
        require(ready==200,'auxiliary fault changed readiness')
        row.update(ready_after=ready,model_after=dict(pid=self.model.pid,**self.model_identity))
        self.faults.append(row);save(self.out/'faults.json',self.faults)
        (control/(name+'.ack')).write_text('done\n')

    def profile_business(self):
        name=self.profile_name;label=name+'-tracing';plan=self.plan
        policy=read(self.out/'client-policy.json');keys={c['id']:hashlib.sha256(os.urandom(32)).hexdigest() for c in policy['clients']}
        self.budget('ingress_starts')
        self.edge=NativeIngress(self.out/label,plan['ingress'],plan['public_port'],plan['port'],
            plan['operations_port'],policy['gateway_token'],keys,plan['tls'],lambda:self.model.poll() is None)
        self.history[self.edge.process.pid]=guard.processes()[self.edge.process.pid]
        def observe(suffix):
            status,raw=self.http('/metrics',label=label+'-'+suffix)
            require(status==200,'actual tracing metrics missing')
            return profile_realtime.state(encrypted.foreground.base.lifecycle.parse_metrics(raw),encrypted.CLIENT)
        before=observe('before');self.budget('websocket_sessions')
        for _ in range(3):self.budget('chats')
        control=self.out/(label+'-control');control.mkdir(mode=0o700)
        source=plan['document_source']
        args=dict(mode=name,base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
            api_key=keys[encrypted.CLIENT],other_api_key=keys[OTHER],ca_file=plan['tls']['certificate'],
            mcp_url=self.peer.url,mcp_token=self.peer.secret,arguments=peer.ARGUMENTS,
            title=source['text'].splitlines()[0].split(': ',1)[1],citation=source['citation'],
            journal_file=str(self.out/(label+'.events.jsonl')),control_dir=str(control))
        if name=='light':args['retained_trace_id']=read(self.out/'primary-tracing.sdk.json')['trace_id']
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','NODE_EXTRA_CA_CERTS')}
        env.update(Q4T_NODE_SDK_DIR=plan['sdk']['sdk_directory'],NODE_EXTRA_CA_CERTS=args['ca_file'])
        receipt=self.out/(label+'.sdk.json');done=set()
        with receipt.open('xb') as stdout,(self.out/(label+'.stderr')).open('xb') as stderr:
            self.actor=subprocess.Popen([plan['sdk']['node']['path'],str(ROOT/'tools/serve_realtime/tracing_sdk.mjs')],
                stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,env=env)
            self.actor.stdin.write(json.dumps(args).encode());self.actor.stdin.close()
            deadline=time.monotonic()+330
            try:
                while self.actor.poll() is None:
                    self.same_model();require(time.monotonic()<deadline,'SDK actor deadline')
                    for fault in ('repair-storage','kill-trace-worker'):
                        if fault not in done and (control/(fault+'.request')).exists():
                            self.fault(fault,control);done.add(fault)
                    time.sleep(.05)
                code=self.actor.returncode
            finally:
                if self.actor.poll() is None:self.actor.terminate()
                try:self.actor.wait(5)
                except subprocess.TimeoutExpired:self.actor.kill();self.actor.wait(3)
                self.actor=None
        require(receipt.stat().st_size<=8<<20,'SDK evidence bound')
        observed=read(receipt);self.same_model();self.drained(label+'-drain')
        after=observe('after-0');end=time.monotonic()+5;index=0
        while (after['sessions'] or after['host_bytes']) and time.monotonic()<end:
            time.sleep(.02);index+=1;after=observe('after-'+str(index))
        remote=self.peer.snapshot();journal=Path(args['journal_file'])
        require(journal.stat().st_size<=8<<20,'journal byte bound')
        value=dict(session=observed,remote=remote,before=before,after=after,source=source)
        save(self.out/(label+'-observed.json'),value)
        require(code in (0,1) and observed['status'] in ('PASS','QUALITY_FAIL'),
                'actual tracing SDK protocol failure; keep original journal and settlement')
        self.observed[name]=tracing_checks.observe(observed,
            [json.loads(line) for line in journal.read_text().splitlines()],remote,source,before,after)
        save(self.out/'observations.json',self.observed)
        status,raw=self.http('/v1/config',label=label+'-final-config')
        require(status==200,'final trace diagnostics unavailable')
        health=json.loads(raw)['realtime_trace_health']
        require(health['confirmed_writes']>0 and health['readiness_dependency'] is False and
            health['model_restart_dependency'] is False,'trace recovered without core dependency')
        save(self.out/(label+'-final-health.json'),health)
        for p in [receipt,journal,*(p for p in self.trace_directory.rglob('*') if p.is_file())]:
            require(all(s.encode() not in p.read_bytes() for s in (*keys.values(),self.peer.secret)),
                    'credential in trace evidence or persistence')
        self.edge.close();save(self.out/(name+'-entry-cleanup.json'),dict(
            exit_code=self.edge.process.returncode,pid=self.edge.process.pid,port_released=True))
        self.edge=None

    def business(self):
        full.Stage.business(self)


def finalize(out):
    plan=read(out/'plan.json');check_plan(plan);result=read(out/'result.json')
    require(result['status']==SUCCESS and 0<result['seconds']<=LIMITS['seconds'] and
        all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
        'bounded tracing business incomplete')
    for n,h in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():
        require(sha(ROOT/n)==h,'postflight source/default drift')
    lease=out/'lease'
    environment=full.environment(read(lease/'exit.json'),
        [json.loads(l) for l in (lease/'nvml-queries.jsonl').read_text().splitlines()],
        [read(out/'primary-cleanup.json'),read(out/'cleanup.json')])
    observations={}
    for name in ('primary','light'):
        value=read(out/(name+'-tracing-observed.json'))
        rows=[json.loads(l) for l in (out/(name+'-tracing.events.jsonl')).read_text().splitlines()]
        observations[name]=tracing_checks.observe(value['session'],rows,value['remote'],
            plan['document_source'],value['before'],value['after'])
        require(read(out/(name+'-entry-cleanup.json'))['exit_code']==0,'native TLS entry cleanup')
    require(observations==read(out/'observations.json'),'postflight observation differs')
    faults=read(out/'faults.json')
    require([f['name'] for f in faults]==['repair-storage','kill-trace-worker'] and
        all(f['model_before']==f['model_after'] and f['ready_after']==200 for f in faults),
        'auxiliary faults changed healthy core')
    full.background.probe_ports(tuple(plan[k] for k in ('port','operations_port','public_port','private_peer_port')))
    with guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    save(out/'acceptance.json',dict(status=SUCCESS,environment=environment,counts=result['counts'],
        seconds=result['seconds'],default_unchanged=True,observations=observations,
        managed_recovery=False,qualification_issued=False,healthy_models_restarted=0,
        model_quality='PASS' if all(q['status']=='PASS' for v in observations.values() for q in v['quality']) else 'FAIL'))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('prepare','run','finalize'))
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output.resolve()
    require(out.is_relative_to(WORK),'private tracing stage directory')
    if args.mode=='prepare':prepare(out)
    elif args.mode=='finalize':finalize(out)
    else:
        def interrupted(_signum,_frame):raise TimeoutError('bounded tracing stage interrupted')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted);signal.alarm(900)
        try:
            with encrypted.foreground.base.lifecycle.child_subreaper():Stage(read(out/'plan.json')).run()
        finally:signal.alarm(0)
