"""Bounded native semantic turn/ASR acceptance under one strict GPU lease."""
import argparse
import base64
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime import history_acceptance as history
from tools.serve_realtime import vad_acceptance as vad
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

WORK = ROOT/'.q4t-work/serve-semantic-vad-20261119'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              committed_items=2, chats=3, controls=2000, seconds=600)
CONTRAST = dict(complete=base.EXPECTED['zh'], incomplete='甚至出现交易几乎')


def settings():
    value = base.settings()
    value['audio']['input']['turn_detection'] = dict(type='semantic_vad', eagerness='high')
    return value


def check_plan(plan):
    require(plan['schema'] == 'q4t-semantic-turn-business-v1' and
            plan['limits'] == LIMITS and plan['settings'] == settings() and
            plan['split_samples'] == 36000 and plan['contrast_text'] == CONTRAST,
            'frozen semantic limits/settings differ')
    require(plan['contract'] == dict(actual_classifier=True,
        usage_includes_private_work=True, preserve_unfinished_turn=True,
        no_auto_replay=True, no_default_changes=True, calibrated=False,
        guardian_recovery=False, master_complete=False), 'semantic contract differs')


def check_score(value):
    choice = value['choices'][0]
    require(len(value['choices']) == 1 and choice['finish_reason'] == 'stop',
            'classifier output incomplete')
    def unique(pairs):
        require(len({key for key, _ in pairs}) == len(pairs), 'duplicate probability key')
        return dict(pairs)
    require(type(choice['message']['content']) is str and
            len(choice['message']['content'].encode()) <= 256, 'classifier content bound')
    score = json.loads(choice['message']['content'], object_pairs_hook=unique)
    require(set(score) == {'p'} and type(score['p']) in (int, float) and
            math.isfinite(score['p']) and 0 <= score['p'] <= 1, 'invalid probability')
    usage = value['usage']
    require(all(type(usage[k]) is int for k in ('prompt_tokens','completion_tokens','total_tokens')) and
            usage['prompt_tokens'] > 0 and 0 < usage['completion_tokens'] <= 16 and
            usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'],
            'classifier actual usage')
    return score['p']


def check_turn(result, continuation):
    require(result['status'] == 'PASS' and result['connections'] == 1 and
            result['close_code'] == 1000 and not result['automatic_replay'],
            'semantic connection boundary')
    terminal = result['terminal']
    base.quality(terminal['transcript'], 'zh'); base.usage(terminal, sdk=True)
    require(result['delta'] == terminal['transcript'], 'semantic stream differs')
    observed = terminal['q4t_semantic_vad']
    for key in ('attempts', 'accepted', 'unavailable', 'invalid', 'stale',
                'input_tokens', 'output_tokens'):
        require(type(observed[key]) is int and observed[key] >= 0, 'invalid classifier counter')
    require(observed['profile'] == 'q4t-native-semantic-turn-v1' and
            0 < observed['accepted'] <= observed['attempts'] <= 8 and
            sum(observed[k] for k in ('accepted', 'unavailable', 'invalid', 'stale')) ==
            observed['attempts'] and observed['included_in_usage'] is True and
            observed['probability_available'] is True and
            observed['maximum_wait'] is False, 'no actual semantic early completion')
    use = terminal['usage']
    require(0 < observed['input_tokens'] < use['input_tokens'] and
            0 < observed['output_tokens'] <= 128 and
            observed['output_tokens'] < use['output_tokens'], 'private usage omitted or duplicated')
    events = result['events']
    require(not any(v['type'] in ('error', 'conversation.item.input_audio_transcription.failed')
                    for v in events), 'server failure cannot be hidden by completion')
    for kind in ('input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped',
                 'input_audio_buffer.committed', 'conversation.item.input_audio_transcription.completed'):
        rows = [v for v in events if v['type'] == kind]
        require(len(rows) == 1 and rows[0]['item_id'] == terminal['item_id'], 'turn events differ')
    if continuation:
        row = result['continuation']
        require(row['no_commit'] is True and row['original_samples_sent'] == 36000 and
                row['pause_samples'] == 12000 and
                row['total_original_samples'] > row['original_samples_sent'],
                'unfinished prefix was not withheld')
    else:
        require(result['continuation'] is None, 'unexpected split')
    item = result['retrieved']['item']
    content = item['content'][0]
    require(item['id'] == terminal['item_id'] and content['transcript'] == terminal['transcript'],
            'retained turn identity differs')
    raw = base64.b64decode(content['audio'], validate=True)
    require(raw and len(raw) % 2 == 0 and len(raw) <= result['sent_samples'] * 2 and
            base64.b64encode(raw).decode() == content['audio'], 'retained PCM contract')
    return use['total_tokens'], dict(actual_classifier=observed,
        actual_usage=use, retained_bytes=len(raw), retained_sha256=hashlib.sha256(raw).hexdigest())


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed semantic source before GPU freeze')
    for name in ('integration-cpu-03', 'sdk-cpu-02', 'driver-cpu-01'):
        require(json.loads((WORK/name/'result.json').read_text())['status'] == 'PASS',
                'semantic targeted CPU not qualified: '+name)
    build = json.loads((WORK/'runtime-build-01/result.json').read_text())
    binary = args.binary.resolve(strict=True)
    require(build['status'] == 'BUILD_PASS' and sha(binary) == build['binary_sha256'],
            'native build identity differs')
    previous = base.WORK/'transcription-business-04/run'
    require(json.loads((previous/'result.json').read_text())['status'] == 'PASS', 'real audio unqualified')
    plan = json.loads((previous/'plan.json').read_text())
    out = args.output.resolve(); require(out.is_relative_to(WORK), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous/'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=10, tokens_per_minute=60000,
                      max_prompt_tokens=32768, max_output_tokens=256)
    save(out/'client-policy.json', policy); (out/'client-policy.json').chmod(0o600)
    tls = certificate(out/'credentials')
    sdk = base.WORK/'profile-sdk-01'
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    reference = WORK/'request-fixture-01'
    requests = {key:json.loads((reference/(key+'.json')).read_text()) for key in CONTRAST}
    plan.update(schema='q4t-semantic-turn-business-v1', limits=LIMITS, output=str(out),
        port=args.port, operations_port=args.port+1, public_port=args.port+2,
        binary=str(binary), binary_sha256=sha(binary),
        sources={n:sha(ROOT/n) for n in sources},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        protected={str(ROOT/n):sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json',
            'build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json')},
        sdk_directory=str(sdk), sdk_files={str(p):sha(p) for p in sdk.rglob('*') if p.is_file()},
        client_policy_sha256=sha(out/'client-policy.json'), tls=tls,
        tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},
        ingress_sha256=sha(plan['ingress']), reused_core_before={},
        vad_directory=str(vad.VAD), vad_assets={vad.WEIGHT:vad.DIGEST},
        settings=settings(), split_samples=36000, contrast_text=CONTRAST,
        contrast_requests=requests, references={str(p):sha(p) for p in reference.iterdir() if p.is_file()},
        contract=dict(actual_classifier=True,usage_includes_private_work=True,
            preserve_unfinished_turn=True,no_auto_replay=True,no_default_changes=True,
            calibrated=False,guardian_recovery=False,master_complete=False),
        scope='One model, two actual WSS semantic utterances, two native-prompt text contrasts and one core Chat; local probability estimate, not OpenAI classifier equivalence or performance acceptance.')
    check_plan(plan); save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS
    def __init__(self, plan):
        check_plan(plan); super().__init__(plan)
        require(plan['vad_assets'] == {vad.WEIGHT:vad.DIGEST} and
                sha(Path(plan['vad_directory'])/vad.WEIGHT) == vad.DIGEST, 'VAD asset drift')
        for name, digest in plan['references'].items():
            require(sha(name) == digest, 'production request reference drift')

    def budget(self, kind, amount=1):
        require(shutil.disk_usage(self.out).free >= 128 << 20, 'disk headroom')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file()) <=
                32 << 20, 'artifact bound')
        super().budget(kind, amount)

    def extra_model_args(self):
        return ['--vad-model-dir', self.plan['vad_directory'], '--asr-max-output-tokens', '256']

    def contrast(self, name):
        self.budget('chats')
        request = self.plan['contrast_requests'][name]
        save(self.out/(name+'.request.json'), request)
        conn = http.client.HTTPConnection('127.0.0.1',self.plan['port'],timeout=30)
        try:
            conn.request('POST','/v1/chat/completions',json.dumps(request,ensure_ascii=False).encode(),
                {'Content-Type':'application/json','X-Q4T-Client-Id':'beta','X-Q4T-Gateway-Token':self.token})
            response = conn.getresponse(); raw = response.read((1 << 20)+1)
            (self.out/(name+'.wire')).write_bytes(raw)
            require(response.status == 200 and len(raw) <= 1 << 20, 'contrast HTTP status/size')
            value = json.loads(raw)
            score = check_score(value)
            self.contrast_tokens += value['usage']['total_tokens']
            return score
        finally:
            conn.close()

    def semantic_sdk(self, name, split):
        self.budget('websocket_sessions'); self.budget('committed_items')
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'],ca_file=self.plan['tls']['certificate'],
            file=self.plan['clips']['zh']['path'],split_samples=split,settings=self.plan['settings'])
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR']=self.plan['sdk_directory']
        run=subprocess.run(['node',str(ROOT/'tools/serve_realtime/semantic_sdk.mjs')],
            input=json.dumps(args),text=True,capture_output=True,env=env,timeout=135)
        (self.out/(name+'.sdk.json')).write_text(run.stdout)
        (self.out/(name+'.stderr')).write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= 6 << 20, 'semantic SDK failed: '+name)
        return json.loads(run.stdout)

    def business(self):
        self.snapshot('before')
        status,raw=self.http('/v1/capabilities',label='capabilities')
        require(status==200 and json.loads(raw)['realtime']['semantic_vad']['configured'],
                'semantic classifier not configured')
        self.contrast_tokens=0
        scores={name:self.contrast(name) for name in CONTRAST}
        save(self.out/'contrast-scores.json',scores)
        require(scores['complete'] > scores['incomplete'], 'meaning contrast not distinguished')
        history.settlement(self.snapshot('contrast-drained'),'beta',self.contrast_tokens,2)
        total=0; observed={}
        for index,(name,split) in enumerate((('complete',0),('continuation',self.plan['split_samples'])),1):
            result=self.semantic_sdk(name,split)
            tokens,observed[name]=check_turn(result,bool(split));total+=tokens
            history.settlement(self.snapshot(name+'-drained'),'alpha',total,index)
        save(self.out/'observed.json',observed)
        self.core('core-after'); self.snapshot('final-drained')
        status,raw=self.http('/v1/config',label='final-config')
        require(status==200 and json.loads(raw)['native_asr_health']['compute_failures']==0,
                'native ASR failure')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--binary',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True);prep.add_argument('--port',type=int,default=18388)
    run=sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='prepare':prepare(args)
    else:
        with base.lifecycle.child_subreaper():Stage(json.loads(args.plan.read_text())).run()
