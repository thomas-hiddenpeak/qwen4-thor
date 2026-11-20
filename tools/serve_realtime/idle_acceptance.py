"""One bounded idle follow-up; preserve original failures and reuse manual work."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime import duplex_acceptance as duplex
from tools.serve_realtime import duplex_checks, idle_checks
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

WORK = duplex.WORK
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1, responses=2,
              audio_inputs=1, chats=6, controls=2000, seconds=900)
CONTRACT = dict(seeds=[41,42], temperature=1, original_failure_preserved=True,
                production_prompt_pair=True, manual_business_reused=True,
                default_unchanged=True, automatic_replay=False,
                guardian_recovery=False, master_complete=False)


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-idle-followup-v1' and
            plan['limits'] == LIMITS and plan['contract'] == CONTRACT,
            'frozen idle follow-up drift')
    idle_checks.prompt_pair(plan['prompts']['before'], plan['prompts']['after'])


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            'commit reviewed source before freezing')
    previous = WORK/'business-02'
    plan = json.loads((previous/'run/plan.json').read_text())
    original = json.loads((previous/'run/result.json').read_text())
    require(original['status'] == 'FAIL' and original['default_unchanged'] and
            json.loads((previous/'lease/exit.json').read_text())['status'] ==
            'COMMAND_FAILED', 'original failure missing')
    queries = [json.loads(l) for l in (previous/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(len(queries) == 82 and all(q['status']=='OK' for q in queries),
            'reused environment invalid')
    manual = json.loads((previous/'run/manual.sdk.json').read_text())
    observed = duplex_checks.observe(manual, plan['expected']['zh'], plan['record'])
    require(all(q['status']=='PASS' for q in observed['quality']), 'manual quality reuse')
    build, cpu = WORK/'idle-fact-runtime-01', WORK/'idle-fact-cpu-01'
    built = json.loads((build/'result.json').read_text())
    require(built['status']=='BUILD_PASS' and built['warnings']==0 and
            sha(args.binary)==built['binary_sha256'], 'native build identity')
    native = json.loads((build/'plan.json').read_text())['sources']
    changed = [n for n,h in native.items() if plan['sources'].get(n)!=h]
    require(changed == ['src/server/realtime_conversation.cpp'],
            'manual reuse requires only native idle fact to differ')
    old = subprocess.check_output(['git','show',plan['source_commit']+':'+changed[0]],cwd=ROOT,text=True)
    actual = (ROOT/changed[0]).read_text()
    expected = old.replace('    // Execution snapshot only. Client transcripts never gain this authority.\n',
        '    // Execution snapshot only. Client transcripts never gain this authority.\n'
        "    // Report the observed input, without adding a response policy that could\n"
        "    // compete with the caller's instructions.\n").replace(
        'interval. Invite the user to continue.]', 'interval.]')
    require(actual == expected, 'non-idle runtime path changed')
    require(json.loads((cpu/'result.json').read_text())['status']=='PASS',
            'targeted CPU not qualified')
    for directory in (build,cpu):
        for n,h in json.loads((directory/'plan.json').read_text())['sources'].items():
            require(sha(ROOT/n)==h, 'qualified source drift')
    probe = WORK/'idle-prompt-01'
    prompts = {name:json.loads((probe/(name+'.json')).read_text()) for name in ('before','after')}
    idle_checks.prompt_pair(prompts['before'],prompts['after'])
    out = args.output.resolve();require(out.is_relative_to(WORK), 'private stage path')
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    policy = json.loads((previous/'run/client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    save(out/'client-policy.json',policy);(out/'client-policy.json').chmod(0o600)
    tls = certificate(out/'credentials')
    sources = list(plan['sources']) + ['tools/serve_realtime/'+n for n in
        ('idle_acceptance.py','idle_checks.py','test_idle_acceptance.py')]
    files = [previous/'run/result.json', previous/'run/manual.sdk.json',
             previous/'run/quality.json', previous/'lease/exit.json',
             previous/'lease/nvml-queries.jsonl']
    files += [p for directory in (build,cpu,probe) for p in directory.iterdir()
              if p.is_file() and p.suffix in ('.json','.cpp','.log','.txt','.stdout','.stderr','.prompt')]
    plan.update(schema='q4t-realtime-idle-followup-v1',limits=LIMITS,contract=CONTRACT,
        output=str(out),port=args.port,operations_port=args.port+1,public_port=args.port+2,
        binary=str(args.binary.resolve()),binary_sha256=sha(args.binary),
        sources={n:sha(ROOT/n) for n in sorted(set(sources))},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        protected={n:sha(n) for n in plan['protected']},qualified_files={str(p):sha(p) for p in files},
        prompts=prompts,client_policy_sha256=sha(out/'client-policy.json'),tls=tls,
        tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},scope=__doc__)
    check_plan(plan);save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS
    extra_model_args = duplex.Stage.extra_model_args

    def __init__(self, plan):
        check_plan(plan);super().__init__(plan)
        for n,h in {**plan['qualified_files'],**plan['tts_assets']}.items():
            require(sha(n)==h, 'qualification/TTS drift')
        for n,h in plan['vad_assets'].items():
            require(sha(Path(plan['vad_directory'])/n)==h, 'VAD drift')

    def diagnostic(self, variant, seed):
        self.budget('chats')
        label = variant+'-seed-'+str(seed)
        request = copy.deepcopy(self.plan['prompts'][variant]['request'])
        request['seed'] = seed
        save(self.out/(label+'-request.json'),request)
        conn = http.client.HTTPConnection('127.0.0.1',self.plan['port'],timeout=120)
        try:
            conn.request('POST','/v1/chat/completions',json.dumps(request),headers={
                'Content-Type':'application/json','X-Q4T-Client-Id':'gamma',
                'X-Q4T-Gateway-Token':self.token})
            response=conn.getresponse();raw=response.read(1048577)
            (self.out/(label+'.wire')).write_bytes(raw)
            require(response.status==200 and len(raw)<=1048576,'diagnostic HTTP')
            value=json.loads(raw)['choices'][0]
            require(value['finish_reason']=='stop', 'diagnostic unfinished')
            row=duplex_checks.judgment(label,value['message'].get('content') or '', 'READY')
            row['refusal']=value['message'].get('refusal')
        finally:conn.close()
        self.same_model();return row

    def idle(self):
        self.budget('websocket_sessions');self.budget('responses',2);self.budget('audio_inputs')
        clip=self.plan['clips']['zh']
        args=dict(mode='idle',base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'],ca_file=self.plan['tls']['certificate'],
            clip=clip['path'],clip_sha256=clip['sha256'],expected=self.plan['expected']['zh'],
            journal_file=str(self.out/'idle.events.jsonl'))
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR']=self.plan['sdk_directory']
        run=duplex.capture_actor(['node',str(ROOT/'tools/serve_realtime/duplex_sdk.mjs')],args,
            self.out/'idle.sdk.json',self.out/'idle.stderr',env,430)
        require(run.returncode==0 and (self.out/'idle.sdk.json').stat().st_size<=16<<20,
                'actual idle actor failed')
        value=json.loads((self.out/'idle.sdk.json').read_text())
        duplex_checks.journal_matches([json.loads(l) for l in
            (self.out/'idle.events.jsonl').read_text().splitlines()],value)
        observed=idle_checks.observe(value,args['expected'])
        self.same_model()
        settlement=duplex.accounting(self.snapshot('idle-drained'),'alpha',observed)
        save(self.out/'idle-receipt.json',dict(observed=observed,settlement=settlement))
        return observed['quality']

    def business(self):
        self.snapshot('before');self.core('core-before')
        diagnostics=[]
        for seed in CONTRACT['seeds']:
            for variant in ('before','after'):
                diagnostics.append(self.diagnostic(variant,seed))
                save(self.out/'diagnostics.json',diagnostics)
        quality=self.idle();save(self.out/'quality.json',quality)
        self.core('core-after');self.snapshot('final-drained')
        require(all(q['status']=='PASS' for q in quality), 'original idle quality failure retained')
        require(all(q['status']=='PASS' for q in diagnostics if q['label'].startswith('after')),
                'candidate fixed-seed diagnostic failure retained')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--binary',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True);prep.add_argument('--port',type=int,default=18508)
    run=sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='prepare':prepare(args)
    else:
        with base.lifecycle.child_subreaper():Stage(json.loads(args.plan.read_text())).run()
