"""Bounded real native Realtime Chat, read-only project tool and ASR regression."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.history_acceptance import settlement
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

WORK = ROOT / '.q4t-work/serve-realtime-conversation-20261119'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=3,
              response_creates=8, committed_items=1, chats=2,
              controls=2000, seconds=900)
CONTRACT = dict(actual_model=True, actual_project_tool=True,
                delta_cancel=True, actual_usage_settled=True,
                shared_asr_regression=True, automatic_replay=False,
                guardian_recovery=False, default_unchanged=True,
                master_complete=False)


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-conversation-business-v1' and
            plan['limits'] == LIMITS and plan['contract'] == CONTRACT,
            'frozen scope drift')
    require(plan['record']['path'] == str(ROOT / 'docs/STATUS.md') and
            re.fullmatch(r'rev[0-9]+', plan['record']['version']),
            'actual project source required')


def response_usage(response):
    value = response['usage']
    for name in ('input_tokens', 'output_tokens', 'total_tokens'):
        require(type(value[name]) is int and value[name] > 0,
                'actual positive model usage required')
    require(value['total_tokens'] == value['input_tokens'] +
            value['output_tokens'], 'usage arithmetic')
    return value['total_tokens']


def check_receipt(receipt, mode, record):
    expected = ['completed', 'failed'] if mode == 'quota' else [
        'completed', 'completed', 'completed', 'incomplete',
        'cancelled', 'completed']
    require(receipt['status'] == 'PASS' and receipt['connections'] == 1 and
            receipt['close_code'] == 1000 and not receipt['automatic_replay'] and
            receipt['transport'] == 'official-openai-wss' and
            receipt['mode'] == mode and
            receipt['response_creates'] == len(expected), 'actual SDK boundary')
    rows = receipt['responses']
    require([r['status'] for r in rows] == expected, 'real response outcomes')
    ids = [r['id'] for r in rows]
    require(len(ids) == len(set(ids)), 'replayed response identity')
    events = receipt['events']
    require([e['response'] for e in events if e['type'] == 'response.done'] ==
            rows, 'terminal evidence differs or repeats')
    total = sum(response_usage(row) for row in rows if row['status'] != 'failed')
    if mode == 'quota':
        require(rows[1]['status_details']['error']['code'] ==
                'client_rpm_exhausted', 'actual quota refusal required')
    else:
        require(receipt['tool_record'] == dict(record_key='release',
                    version=record['version'], source='docs/STATUS.md',
                    source_sha256=record['sha256']), 'actual tool provenance')
        require(rows[3]['status_details']['reason'] == 'max_output_tokens' and
                rows[3]['usage']['output_tokens'] == 1,
                'exact output limit required')
        require(any(e['type'] == 'response.output_text.delta' and
                    e.get('response_id') == rows[4]['id'] and e.get('delta')
                    for e in events), 'cancel before model output is not proof')
        require(all(not rows[i].get('conversation_id') for i in (2, 3, 4)),
                'out-of-band responses entered default conversation')
    return total


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed source before freezing actual business')
    build = WORK / 'runtime-build-01'
    result = json.loads((build / 'result.json').read_text())
    build_plan = json.loads((build / 'plan.json').read_text())
    require(result['status'] == 'BUILD_PASS' and result['warnings'] == 0 and
            result['default_unchanged'], 'candidate build not qualified')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == result['binary_sha256'], 'candidate identity changed')
    for name, digest in build_plan['sources'].items():
        require(sha(ROOT / name) == digest, 'built runtime source drift')
    cpu = WORK / 'business-cpu-01'
    require(json.loads((cpu / 'result.json').read_text())['status'] == 'PASS',
            'business verifier CPU not qualified')
    previous = base.WORK / 'transcription-business-04/run'
    require(json.loads((previous / 'result.json').read_text())['status'] == 'PASS',
            'original audio fixture not qualified')
    plan = json.loads((previous / 'plan.json').read_text())
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private phase output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=1 if client['id'] == 'beta' else 20,
                      tokens_per_minute=6000 if client['id'] == 'beta' else 60000,
                      max_output_tokens=256)
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include',
        'cmake', 'CMakeLists.txt', 'tools/serve_realtime',
        'tools/serve_grammar_reap', 'tools/serve_recovery', 'tools/serve_service',
        'tools/evalscope', 'tools/native_asr', 'tools/release'],
        cwd=ROOT, text=True).splitlines()
    protected = [ROOT / p for p in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json',
        'build/service-native-state/ledger.json')]
    record = ROOT / 'docs/STATUS.md'
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'current project release missing')
    sdk = base.WORK / 'profile-sdk-01'
    fixed = {str(p): sha(p) for p in sorted((sdk / 'node_modules').rglob('*'))
             if p.is_file()}
    require(fixed, 'fixed SDK files missing')
    plan.update(schema='q4t-realtime-conversation-business-v1', limits=LIMITS,
        contract=CONTRACT, output=str(out), port=args.port,
        operations_port=args.port + 1, public_port=args.port + 2,
        binary=str(binary), binary_sha256=sha(binary),
        sdk_directory=str(sdk), sdk_files=fixed,
        sources={n: sha(ROOT / n) for n in sources},
        protected={str(p): sha(p) for p in protected},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        reused_core_before={},
        qualified_files={str(p): sha(p) for folder in (build, cpu)
                         for p in folder.iterdir() if p.is_file()},
        record=dict(path=str(record), sha256=sha(record),
                    version='rev' + version.group(1)),
        scope='Native Chat + official WSS/TLS SDK, real read-only project tool, '
              'actual cancel/limit/quota settlement and shared ASR regression; '
              'PID-bound admission fixture, no Guardian qualification or '
              'performance/full-model quality claim.')
    check_plan(plan)
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan)
        super().__init__(plan)
        for name, digest in plan['qualified_files'].items():
            require(sha(name) == digest, 'CPU/build qualification drift')
        require(sha(plan['record']['path']) == plan['record']['sha256'],
                'project release changed after freeze')

    def budget(self, kind, amount=1):
        require(shutil.disk_usage(self.out).free >= 128 << 20, 'disk headroom')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())
                <= 64 << 20, 'artifact budget')
        super().budget(kind, amount)

    def conversation(self, mode, client):
        self.budget('websocket_sessions')
        self.budget('response_creates', 6 if mode == 'main' else 2)
        record = self.plan['record']
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys[client], ca_file=self.plan['tls']['certificate'],
            mode=mode, record_file=record['path'], record_sha256=record['sha256'],
            expected_version=record['version'])
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT /
            'tools/serve_realtime/conversation_sdk.mjs')], input=json.dumps(args),
            text=True, capture_output=True, env=env, timeout=250)
        (self.out / (mode + '.sdk.json')).write_text(run.stdout)
        (self.out / (mode + '.stderr')).write_text(run.stderr)
        require(run.returncode == 0, 'actual conversation SDK failed: ' + mode)
        require(len(run.stdout.encode()) <= 8 << 20, 'SDK receipt bound')
        result = json.loads(run.stdout)
        tokens = check_receipt(result, mode, record)
        self.same_model()
        return tokens

    def business(self):
        self.snapshot('before')
        self.core('core-before')
        tokens = self.conversation('main', 'alpha')
        settlement(self.snapshot('main-drained'), 'alpha', tokens, 6)
        tokens = self.conversation('quota', 'beta')
        state = self.snapshot('quota-drained')
        settlement(state, 'beta', tokens, 1)
        require(state['q4t_client_rate_rejected_total{client="beta",kind="rpm"}']
                == 1, 'actual RPM refusal metric')
        before = state['q4t_client_rate_settled_tokens_total{client="gamma"}']
        clip = dict(file=self.plan['clips']['zh']['path'], settings=base.settings())
        result = self.sdk('asr-regression', 'gamma', [clip])
        terminal = result['turns'][0]['terminal']
        base.quality(terminal['transcript'], 'zh')
        base.usage(terminal, sdk=True)
        settlement(self.snapshot('asr-drained'), 'gamma',
                   before + terminal['usage']['total_tokens'], 2)
        self.core('core-after')
        self.snapshot('final-drained')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final operations config')
        health = json.loads(raw)['native_asr_health']
        require(health['state'] == 'ready' and health['compute_failures'] == 0,
                'shared ASR health regressed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18418)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
