"""One native speech cancellation repair, with no controller or model restart.

The development ingress is killed only after real audio and a held reservation.
The same model then answers an explicit new connection. This verifies the
accounting repair; previous managed recovery observations retain their identity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_realtime import speech_acceptance as first
from tools.serve_realtime import managed, speech_managed as speech
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_service.package import require, save, sha

WORK=first.WORK
LIMITS=dict(starts=1,ingress_starts=2,websocket_sessions=2,response_creates=3,
            chats=0,controls=1800,seconds=420)


def verify_business(value):
    require(value['format']=='q4t-speech-cancellation-repair-v1' and
        value['automatic_replay'] is False and value['model_restarts']==0,
        'unknown cancellation repair or replay')
    before,held,after,final=(value[k] for k in ('before','held','after','final'))
    require(managed.clean(before) and managed.clean(after) and managed.clean(final),
            'audio resources not drained')
    managed.state_shape(held)
    require(held['active']==held['sessions']==1 and held['reserved']>0 and
        held['host_bytes']>0 and held['free_slots']==0 and
        held['admitted']==before['admitted']+2 and held['success']==before['success']+1,
        'fault had no actual active audio owner')
    interrupted=speech.check_session(value['interrupted'],'hold_stream','official-openai-wss')
    complete=speech.check_session(value['complete'],'complete','official-openai-wss')
    require(value['interrupted']['responses'][0]['id']!=value['complete']['responses'][0]['id'],
            'explicit continuation replayed prior response')
    used=speech.accounting(before,after,interrupted,True)
    require(used>interrupted and held['settled']==before['settled']+interrupted,
            'partial actual audio work was not settled')
    speech.accounting(after,final,complete)
    require(value['owner_before']==value['owner_after'] and
        type(value['owner_before']['pid']) is int and value['owner_before']['pid']>0 and
        type(value['owner_before']['start_ticks']) is int and value['owner_before']['start_ticks']>0,
        'healthy core identity changed')
    first.tts_health(value['configuration'],True)
    return dict(completed_tokens=interrupted+complete,
        cancelled_actual_tokens=used-interrupted,aborted_requests=1,
        request_errors=0,automatic_replay=False,model_restarts=0)


def check_plan(plan):
    require(plan['schema']=='q4t-speech-cancellation-repair-v1' and
        plan['limits']==LIMITS and plan['model_restarts']==0 and
        plan['controller_recovery_qualification'] is False,'repair scope drift')


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            'commit reviewed cancellation driver first')
    build=WORK/'runtime-build-02';built=json.loads((build/'result.json').read_text())
    require(built['status']=='BUILD_PASS' and built['warnings']==0,'zero warning repair build required')
    for n,h in json.loads((build/'plan.json').read_text())['sources'].items():
        require(sha(ROOT/n)==h,'built source drift')
    binary=args.binary.resolve(strict=True);require(sha(binary)==built['binary_sha256'],'binary drift')
    prior=WORK/'business-01/run';plan=json.loads((prior/'plan.json').read_text())
    out=args.output.resolve();require(out.is_relative_to(WORK),'private repair output required')
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    policy=json.loads((prior/'client-policy.json').read_text())
    policy['gateway_token']=hashlib.sha256(os.urandom(32)).hexdigest()
    save(out/'client-policy.json',policy);(out/'client-policy.json').chmod(0o600)
    tls=certificate(out/'credentials')
    names=subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/serve_realtime','tools/native_tts','tools/native_asr','tools/serve_grammar_reap',
        'tools/serve_recovery','tools/serve_service','tools/evalscope','tools/release'],cwd=ROOT,text=True).splitlines()
    compiled=Path(json.loads((build/'plan.json').read_text())['command'][2])/'generated/q4t/server/chat_protocol_sources.json'
    source_map=json.loads(compiled.read_text())
    protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(source_map.items())).encode()).hexdigest()
    plan.update(schema='q4t-speech-cancellation-repair-v1',output=str(out),limits=LIMITS,
        binary=str(binary),binary_sha256=sha(binary),protocol_sha256=protocol,
        port=args.port,operations_port=args.port+1,public_port=args.port+2,
        sources={n:sha(ROOT/n) for n in names},
        protected={str(ROOT/n):sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json',
            'build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json')},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        tls=tls,tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},
        client_policy_sha256=sha(out/'client-policy.json'),qualified_files={},reused_core_before={},
        model_restarts=0,controller_recovery_qualification=False,scope=__doc__)
    check_plan(plan);save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(first.base.Stage):
    limits=LIMITS

    def __init__(self,plan):
        check_plan(plan);super().__init__(plan)
        for n,h in plan['tts_assets'].items():require(sha(n)==h,'TTS assets drift')

    extra_model_args=first.Stage.extra_model_args

    def budget(self,kind,amount=1):
        require(shutil.disk_usage(self.out).free>=32<<20,'bounded repair disk reserve')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())<=16<<20,
                'bounded repair artifact size')
        super().budget(kind,amount)

    def observe(self,label,condition):
        rows=[];end=time.monotonic()+15
        while time.monotonic()<end and len(rows)<128:
            self.same_model();status,raw=self.http('/metrics',timeout=1)
            rows.append(dict(http_status=status,body=raw.decode()));save(self.out/(label+'-metrics.json'),rows)
            require(status==200,'actual metrics unavailable');value=managed.state(raw.decode())
            if condition(value):return value
            time.sleep(.02)
        raise TimeoutError('audio ownership observation: '+label)

    def sdk(self,mode):
        self.budget('websocket_sessions');self.budget('response_creates',2 if mode=='hold_stream' else 1)
        args=dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'],ca_file=self.plan['tls']['certificate'],mode=mode)
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR']=self.plan['sdk_directory']
        with (self.out/(mode+'.stderr')).open('xb') as log:
            process=subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/speech_recovery_sdk.mjs')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,env=env)
            reader=managed.Actor(self);reader.process=process
            try:
                process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
                if mode=='hold_stream':
                    ready=reader.line(180);save(self.out/'speech-ready.json',ready)
                    require(ready.get('phase')=='ready' and ready.get('partial'),'actual audio not pending')
                    self.held=self.observe('held',lambda s:s['active']==s['sessions']==1 and s['reserved']>0)
                    pid=self.ingress.process.pid
                    first.base.lifecycle.guard.signal_identity(pid,self.history[pid],signal.SIGKILL)
                    self.ingress.process.wait(5)
                value=reader.line(180 if mode=='complete' else 15)
                save(self.out/(mode+'.sdk.json'),value)
                process.wait(5)
                require(process.returncode==0 and not reader.buffer and not process.stdout.read(1),
                        'SDK failed or repeated receipt')
                speech.check_session(value,mode,'official-openai-wss');self.same_model();return value
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=5)

    def business(self):
        before=self.observe('before',managed.clean)
        owner=dict(pid=self.model.pid,start_ticks=self.model_identity['start_ticks'])
        interrupted=self.sdk('hold_stream');after=self.observe('after',managed.clean)
        self.ingress.close();self.budget('ingress_starts')
        self.ingress=NativeIngress(self.out/'reopened-ingress',self.plan['ingress'],
            self.plan['public_port'],self.plan['port'],self.plan['operations_port'],
            self.token,self.keys,self.plan['tls'],lambda:self.model.poll() is None)
        self.history[self.ingress.process.pid]=first.base.lifecycle.guard.processes()[self.ingress.process.pid]
        complete=self.sdk('complete');final=self.observe('final',managed.clean)
        status,raw=self.http('/v1/config',label='configuration')
        require(status==200 and json.loads(raw)['protocol_source_sha256']==self.plan['protocol_sha256'],
                'actual repaired protocol identity differs')
        value=dict(format=self.plan['schema'],automatic_replay=False,model_restarts=0,
            before=before,held=self.held,after=after,final=final,interrupted=interrupted,complete=complete,
            owner_before=owner,owner_after=dict(pid=self.model.pid,start_ticks=self.model_identity['start_ticks']),
            configuration=json.loads(raw))
        save(self.out/'business.json',value);save(self.out/'verified-business.json',verify_business(value))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--port',type=int,default=18328)
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='prepare':prepare(args)
    else:
        with first.base.lifecycle.child_subreaper():Stage(json.loads(args.plan.read_text())).run()
