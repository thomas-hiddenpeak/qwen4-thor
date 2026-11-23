"""Verify credential ownership across isolated ingress and model generations."""
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.ingress_fixture import NativeIngress
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

SCHEMA = 'q4t-realtime-credential-generation-v1'
FIRST = dict(starts=1, ingress_starts=3, issues=2, handshakes=4, chats=1, controls=1500, seconds=160)
SECOND = dict(starts=1, ingress_starts=1, issues=1, handshakes=2, chats=1, controls=1500, seconds=140)


def prepare(out, qualification):
    live.prepare(out, qualification)
    primary = read(out/'plan.json')
    for i, limits in enumerate((FIRST, SECOND), 1):
        directory = out/f'generation-{i}'
        directory.mkdir(mode=0o700)
        shutil.copy2(out/'client-policy.json', directory/'client-policy.json')
        # The public address, TLS certificate, principal keys and gateway binding
        # deliberately stay identical. Only the private model process changes.
        plan = dict(primary, schema=SCHEMA, limits=limits, output=str(directory),
                    public_port=18803, port=18804+2*i, operations_port=18805+2*i)
        save(directory/'plan.json', plan)


class Generation(live.Stage):
    def __init__(self, plan, previous=None):
        self.previous = previous
        self.retained = None
        self.limits = SECOND if previous else FIRST
        super().__init__(plan)

    def issue(self, owner):
        self.budget('issues')
        ctx = ssl.create_default_context(cafile=self.plan['tls']['certificate'])
        conn = http.client.HTTPSConnection('127.0.0.1', self.plan['public_port'], context=ctx,timeout=8)
        try:
            body = dict(expires_after=dict(seconds=600),session=dict(type='realtime',
                        output_modalities=['text'], instructions='recovery '+owner))
            conn.request('POST','/v1/realtime/client_secrets',json.dumps(body),
                         {'Authorization':'Bearer '+self.keys[owner],'Content-Type':'application/json'})
            resp = conn.getresponse()
            wire = resp.read(65537)
            require(resp.status==200 and len(wire)<=65536,'credential issue at current generation')
            result = json.loads(wire)
            require(result['session']['instructions']=='recovery '+owner,'issue identity')
            return (result['value'], result['expires_at'])
        finally:
            conn.close()

    def probe(self, grant, owner, expected):
        self.budget('handshakes')
        require(grant[1] > time.time()+30,'credential must remain unexpired')
        actor = subprocess.run(['node',ROOT/'tools/serve_live/media_create_sdk.mjs'],
            input=json.dumps(dict(action='credential-session',key=grant[0],
                 base_url=f'https://127.0.0.1:{self.plan["public_port"]}/v1')),
            text=True,capture_output=True,timeout=15,
            env=dict(os.environ,Q4T_NODE_SDK_DIR=self.plan['sdk_directory'],
                     NODE_EXTRA_CA_CERTS=self.plan['tls']['certificate']))
        require(actor.returncode==0 and len(actor.stdout)<4096,'bounded official SDK handshake')
        seen = json.loads(actor.stdout)
        require(seen['status']==expected,'credential handshake got '+str(seen['status']))
        if expected==101:
            require(seen['instructions']=='recovery '+owner and seen['session'].startswith('sess_'),
                    'matching private startup settings')
        return seen

    def replace_ingress(self, suffix, keys, *, crash=False):
        old = self.ingress.process
        if crash:
            row = self.history[old.pid]
            base.lifecycle.guard.signal_identity(old.pid,row,signal.SIGKILL)
            require(old.wait(timeout=5)==-signal.SIGKILL,'owned ingress exit')
        self.ingress.close()
        self.ingress = None
        self.same_model()
        self.budget('ingress_starts')
        self.ingress = NativeIngress(self.out/suffix,self.plan['ingress'],self.plan['public_port'],
            self.plan['port'],self.plan['operations_port'],self.token,keys,self.plan['tls'],
            lambda:self.model.poll() is None)
        pid = self.ingress.process.pid
        self.history[pid] = base.lifecycle.guard.processes()[pid]
        require(pid != old.pid,'new ingress process')
        self.same_model()

    def business(self):
        observation = {}
        if self.previous is None:
            alpha = self.issue('alpha')
            self.retained = self.issue('beta')
            first = self.probe(alpha,'alpha',101)
            self.snapshot('first-drained')
            self.replace_ingress('after-ingress-exit',self.keys,crash=True)
            second = self.probe(alpha,'alpha',101)
            require(first['session']!=second['session'],'new connection must have a fresh history')
            self.snapshot('reconnected-drained')
            self.replace_ingress('rotated-policy',dict(self.keys,alpha=os.urandom(32).hex()))
            observation['revoked_issuer'] = self.probe(alpha,'alpha',401)['status']
            observation['other_issuer'] = self.probe(self.retained,'beta',101)['status']
            observation.update(same_model=True,new_session_after_ingress_exit=True)
        else:
            observation['previous_store'] = self.probe(self.previous,'beta',401)['status']
            current = self.issue('beta')
            observation['new_store'] = self.probe(current,'beta',101)['status']
        self.snapshot('credential-drained')
        self.core('core-after')
        self.snapshot('final-drained')
        save(self.out/'observation.json',observation)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','finalize'))
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),'private generation evidence path')
    if args.action=='prepare':
        prepare(out,args.qualification)
        return
    dirs=[out/'generation-1',out/'generation-2']
    plans=[read(d/'plan.json') for d in dirs]
    require(all(p['schema']==SCHEMA for p in plans),'recovery plan identity')
    if args.action=='run':
        with base.lifecycle.child_subreaper():
            first=Generation(plans[0])
            first.run()
            require(first.retained,'original grant required')
            Generation(plans[1], first.retained).run()
        return
    phases=[]
    for directory,plan,limits in zip(dirs,plans,(FIRST,SECOND)):
        result=read(directory/'result.json')
        require(result['status']=='PASS' and result['default_unchanged'] and result['seconds']<=limits['seconds']
                and all(result['counts'][k]==v for k,v in limits.items() if k not in ('seconds','controls')),
                'incomplete lifecycle verification')
        require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()),'qualified sources')
        phases.append(dict(seconds=result['seconds'],counts=result['counts'],
                           observation=read(directory/'observation.json')))
    a,b=(read(d/'model-identity.json') for d in dirs)
    require((a['pid'],a['start_ticks'])!=(b['pid'],b['start_ticks']),'distinct engine identity')
    env=qualify.environment(read(out/'lease/exit.json'),
        [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
        [read(d/'cleanup.json') for d in dirs],expected_starts=2)
    save(out/'acceptance.json',dict(environment=env,phases=phases,core=['42','42'],
        identical_binding=plans[0]['client_policy_sha256']==plans[1]['client_policy_sha256'],
        managed_supervisor=False,default_unchanged=True))


if __name__=='__main__':
    main()
