"""Bounded real history input, deletion and processed-audio retrieval acceptance.

Uses the actual native TLS ingress and pinned OpenAI SDK. The development
admission controller remains PID-bound; Guardian recovery is a separate task.
"""
import argparse
import base64
import hashlib
import json
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
from tools.serve_realtime import streaming_acceptance as streaming
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              committed_items=4, chats=1, controls=2000, seconds=600)
ARTIFACT_BYTES = 64 << 20
HEADROOM_BYTES = 256 << 20


def settings(mode):
    require(mode in ('manual', 'near_stream'), 'fixed history mode')
    value = base.settings('audio/pcm' if mode == 'manual' else 'audio/pcmu')
    if mode == 'near_stream':
        value['audio']['input']['transcription']['delay'] = 'low'
        value['audio']['input']['noise_reduction'] = dict(type='near_field')
    return value


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-history-business-v1' and
            plan['limits'] == LIMITS and plan['settings'] == {
                mode: settings(mode) for mode in ('manual', 'near_stream')},
            'history limits/settings differ')
    require(plan['reference']['history_tokens'] > 0 and
            plan['reference']['base_input_tokens'] > 0 and
            len(plan['reference']['token_ids']) == plan['reference']['history_tokens'],
            'independent history token reference missing')
    require(plan['contract'] == dict(actual_waveform_bytes=True,
        actual_history_input_tokens=True, delete_before_third_item=True,
        uncommitted_hidden=True, foreign_session_hidden=True,
        actual_usage_settled=True, guardian_recovery=False,
        default_unchanged=True, master_complete=False), 'fixed history contract')


def check_retrieved(turn, expected):
    item = turn['retrieved']['item']
    terminal = turn['terminal']
    require(item['id'] == turn['item_id'] == terminal['item_id'] and
            item['object'] == 'realtime.item' and item['type'] == 'message' and
            item['role'] == 'user' and item['status'] == 'completed' and
            len(item['content']) == 1 and
            item['content'][0]['type'] == 'input_audio', 'retrieved item identity')
    content = item['content'][0]
    require(content['transcript'] == terminal['transcript'] == turn['delta'],
            'retained transcript differs from actual ASR')
    encoded = content['audio']
    require(type(encoded) is str and len(encoded) <= 4 << 20,
            'retrieved base64 bound')
    raw = base64.b64decode(encoded, validate=True)
    require(base64.b64encode(raw).decode() == encoded and
            len(raw) == expected['bytes'] and
            hashlib.sha256(raw).hexdigest() == expected['sha256'],
            'retrieved audio differs from actual processed waveform')
    base.usage(terminal, sdk=True)
    base.quality(terminal['transcript'], 'zh')
    return raw


def check_history(result, reference):
    require(result['status'] == 'PASS' and result['connections'] == 1 and
            not result['automatic_replay'] and len(result['turns']) == 3,
            'history session boundary')
    turns = result['turns']
    require(len({v['item_id'] for v in turns}) == 3, 'history identity reused')
    require(turns[0]['terminal']['transcript'] == reference['prior_transcript'],
            'actual first text differs from frozen independent token reference')
    counts = [v['terminal']['usage']['input_tokens'] for v in turns]
    initial = reference['base_input_tokens']
    require(counts == [initial, initial + reference['history_tokens'], initial],
            'actual history input or deletion did not change model usage')
    require(turns[1]['committed']['previous_item_id'] == turns[0]['item_id'] and
            turns[2]['committed']['previous_item_id'] is None,
            'actual history predecessor differs')
    checks = result['checks']
    deleted = [v['item_id'] for v in checks if v['type'] == 'deleted']
    missing = [v['item_id'] for v in checks if v['type'] == 'absent']
    require(deleted == missing == [v['item_id'] for v in turns[:2]],
            'both actual prior items must be deleted and absent')
    return sum(v['terminal']['usage']['total_tokens'] for v in turns)


def settlement(values, client, tokens, admitted):
    key = lambda name: name + '{client="' + client + '"}'
    require(values[key('q4t_client_rate_admitted_total')] == admitted and
            values[key('q4t_client_rate_settled_tokens_total')] == tokens and
            values[key('q4t_client_rate_reserved_tokens')] == 0,
            'actual per-item admission or token settlement differs')


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed source before GPU freeze')
    cpu = base.WORK / 'history-retrieve-cpu-01'
    qualification = json.loads((cpu / 'result.json').read_text())
    require(qualification['status'] == 'PASS', 'history runtime CPU not qualified')
    driver = args.references.resolve(strict=True)
    require(json.loads((driver / 'result.json').read_text())['status'] == 'PASS',
            'history driver CPU not qualified')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == qualification['runtime_sha256'], 'runtime identity differs')
    previous = base.WORK / 'transcription-business-04/run'
    plan = json.loads((previous / 'plan.json').read_text())
    require(json.loads((previous / 'result.json').read_text())['status'] == 'PASS',
            'fixed clean clips not qualified')
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default drift')
    out = args.output.resolve()
    require(out.is_relative_to(base.WORK), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=10, tokens_per_minute=10000,
                      max_output_tokens=256)
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    reference = json.loads((driver / 'token-reference.json').read_text())
    waveforms = json.loads((driver / 'waveforms.json').read_text())
    qualified = {str(p): sha(p) for p in driver.iterdir() if p.is_file()}
    plan.update(schema='q4t-realtime-history-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port + 1,
        public_port=args.port + 2, binary=str(binary), binary_sha256=sha(binary),
        sources={n: sha(ROOT / n) for n in sources},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        reference=reference, waveforms=waveforms, qualified_reference_files=qualified,
        settings={mode: settings(mode) for mode in ('manual', 'near_stream')},
        contract=dict(actual_waveform_bytes=True, actual_history_input_tokens=True,
            delete_before_third_item=True, uncommitted_hidden=True,
            foreign_session_hidden=True, actual_usage_settled=True,
            guardian_recovery=False, default_unchanged=True, master_complete=False),
        scope='Four real ASR items, actual retained waveform, frozen independent history token delta and deletion, native TLS/SDK, one core Chat. Fixed-PID admission fixture, not Guardian; no performance/full-quality claim.')
    check_plan(plan)
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan)
        super().__init__(plan)
        for name, digest in plan['qualified_reference_files'].items():
            require(sha(name) == digest, 'frozen reference evidence drift')
        for row in plan['waveforms'].values():
            require(sha(row['path']) == row['sha256'], 'waveform reference drift')

    def budget(self, kind, amount=1):
        require(shutil.disk_usage(self.out).free >= HEADROOM_BYTES, 'disk headroom')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file()) <=
                ARTIFACT_BYTES, 'artifact budget')
        super().budget(kind, amount)

    def extra_model_args(self):
        return ['--asr-max-output-tokens', '256']

    def history_sdk(self, mode, foreign_id=None):
        self.budget('websocket_sessions')
        self.budget('committed_items', 3 if mode == 'manual' else 1)
        client = 'alpha' if mode == 'manual' else 'gamma'
        clip = self.plan['clips']['zh' if mode == 'manual' else 'mulaw']
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys[client], ca_file=self.plan['tls']['certificate'],
            file=clip['path'], mode=mode, settings=self.plan['settings'][mode],
            foreign_id=foreign_id)
        env = {k:v for k,v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT / 'tools/serve_realtime/history_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=135)
        (self.out / (mode + '.sdk.json')).write_text(run.stdout)
        (self.out / (mode + '.stderr')).write_text(run.stderr)
        require(run.returncode == 0, 'history SDK failed: ' + mode)
        require(len(run.stdout.encode()) <= 20 << 20, 'SDK receipt bound')
        result = json.loads(run.stdout)
        require(result['status'] == 'PASS' and result['connections'] == 1 and
                not result['automatic_replay'], 'actual SDK boundary')
        self.same_model()
        return result

    def business(self):
        self.snapshot('before')
        result = self.history_sdk('manual')
        total = check_history(result, self.plan['reference'])
        for i, turn in enumerate(result['turns']):
            raw = check_retrieved(turn, self.plan['waveforms']['manual'])
            (self.out / f'manual-{i}.retrieved.pcm').write_bytes(raw)
        settlement(self.snapshot('manual-drained'), 'alpha', total, 3)
        last = result['turns'][-1]['item_id']
        save(self.out / 'history-observed.json', dict(
            actual_input=[t['terminal']['usage']['input_tokens'] for t in result['turns']],
            reference_delta=self.plan['reference']['history_tokens'], actual_tokens=total))
        result = self.history_sdk('near_stream', last)
        require(len(result['turns']) == 1, 'continuous item count')
        turn = result['turns'][0]
        missing = [(v['label'], v['item_id']) for v in result['checks'] if v['type'] == 'absent']
        require(missing == [('foreign-session', last), ('uncommitted', turn['item_id'])],
                'cross-session/precommit visibility was not checked')
        raw = check_retrieved(turn, self.plan['waveforms']['near_stream'])
        (self.out / 'near-stream.retrieved.mulaw').write_bytes(raw)
        self.observed = []
        # Reuse the already qualified native-step/actual-usage verifier.
        tokens = streaming.Stage.completed(self, turn, 'mulaw', 'low', 24000)
        settlement(self.snapshot('stream-drained'), 'gamma', tokens, 1)
        self.core('core-after')
        state = self.snapshot('final-drained')
        require(state['q4t_requests_total'] == 5 and
                state['q4t_requests_success_total'] == 5 and
                state['q4t_requests_error_total'] == 0 and
                state['q4t_requests_aborted_total'] == 0, 'actual outcome totals')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final config')
        health = json.loads(raw)['native_asr_health']
        require(health['state'] == 'ready' and health['compute_failures'] == 0,
                'ASR health changed')

    steps = streaming.Stage.steps


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--references', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18378)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
