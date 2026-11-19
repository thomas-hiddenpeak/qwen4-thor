"""One owned GPU stage for actual Realtime Prompt, files, tools and accounting."""
import argparse
import copy
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

WORK = ROOT / '.q4t-work/serve-realtime-prompt-20261120'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              response_creates=13, chats=2, http_operations=32,
              controls=2000, seconds=900)
CONTRACT = dict(actual_model=True, actual_files=True, actual_sheet_model=True,
                actual_project_tool=True, pinned_prompt_version=True,
                client_isolation=True, one_rpm_per_response=True,
                actual_usage_settled=True, delta_cancel=True,
                automatic_replay=False, guardian_recovery=False,
                default_unchanged=True, master_complete=False)
CASES = dict(alpha=['pinned-first', 'one-response-second', 'one-response-null',
    'actual-project-file', 'actual-image', 'actual-csv', 'actual-tool-call',
    'pinned-tool-after-delete', 'cancel-after-delta', 'new-request-after-cancel'],
    beta=['foreign-file-denied', 'beta-csv-one-rpm', 'beta-rpm-denied'])


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-prompt-business-v1' and
            plan['limits'] == LIMITS and plan['contract'] == CONTRACT,
            'frozen Prompt scope drift')
    require(plan['record']['path'] == str(ROOT / 'docs/STATUS.md') and
            re.fullmatch(r'rev[0-9]+', plan['record']['version']),
            'actual project source required')
    require(plan['image']['path'] == str(ROOT / 'tests/fixtures/file_images/original.png')
            and plan['image']['expected_left_color'] == 'red', 'reviewed image source')
    rows = plan['csv_sources']
    require(len(rows) == 2 and all(type(r['tests']) is int and r['tests'] > 0
                                 for r in rows) and
            plan['csv_sum'] == sum(r['tests'] for r in rows), 'actual CSV derivation')


def text(response):
    return ''.join(part['text'] for item in response['output']
                   if item['type'] == 'message' for part in item['content']
                   if part['type'] == 'output_text')


def check_receipt(receipt, mode, record, csv_sum):
    require(receipt['status'] == 'PASS' and receipt['mode'] == mode and
            receipt['transport'] == 'official-openai-wss' and
            receipt['connections'] == 1 and receipt['close_code'] == 1000 and
            receipt['automatic_replay'] is False and
            type(receipt['http_calls']) is int and 1 <= receipt['http_calls'] <= 16,
            'actual SDK connection/HTTP boundary')
    rows = receipt['responses']; events = receipt['events']
    require(receipt['response_creates'] == len(CASES[mode]) == len(rows) and
            [r['metadata']['acceptance_case'] for r in rows] == CASES[mode],
            'exact response scenario identity')
    require(len({r['id'] for r in rows}) == len(rows) and
            [e['response'] for e in events if e['type'] == 'response.done'] == rows,
            'terminal absent, altered or replayed')
    total = 0
    for index, row in enumerate(rows):
        expected = ('cancelled' if index == 8 else 'completed') if mode == 'alpha' \
            else ('completed' if index == 1 else 'failed')
        require(row['status'] == expected, 'actual terminal state')
        value = row['usage']
        require(all(type(value[k]) is int and value[k] >= 0 for k in
                    ('input_tokens', 'output_tokens', 'total_tokens')) and
                value['total_tokens'] == value['input_tokens'] + value['output_tokens'],
                'actual usage arithmetic')
        if expected == 'failed':
            code = 'object_not_found' if index == 0 else 'client_rpm_exhausted'
            require(row['status_details']['error']['code'] == code and
                    value['total_tokens'] == 0, 'unstarted denial is not model work')
        else:
            require(value['input_tokens'] > 0 and value['output_tokens'] > 0,
                    'positive actual model usage')
            total += value['total_tokens']
        if expected == 'completed':
            deltas = ''.join(e['delta'] for e in events if
                e['type'] == 'response.output_text.delta' and e['response_id'] == row['id'])
            require(deltas == text(row), 'actual streamed text differs')
    if mode == 'beta':
        require(text(rows[1]).strip() == str(csv_sum), 'actual beta sheet quality')
        require(any(e['type'] == 'error' and
                    e['error'].get('event_id') == 'foreign-prompt-denied' and
                    e['error']['code'] == 'object_not_found' for e in events),
                'actual cross-client Prompt denial absent')
        return total
    expected = {0: '42', 1: '377', 2: '72', 3: record['version'], 4: 'red',
                5: str(csv_sum), 7: record['version'], 9: '56'}
    for index, answer in expected.items():
        require(text(rows[index]).strip() == answer, 'actual Prompt business quality')
    source = dict(version=record['version'], source='docs/STATUS.md',
                  source_sha256=record['sha256'])
    require(receipt['source_record'] == source and
            receipt['tool_record'] == dict(record_key='release', **source),
            'actual project source provenance')
    calls = [i for i in rows[6]['output'] if i['type'] == 'function_call']
    require(len(calls) == 1 and calls[0]['status'] == 'completed' and
            calls[0]['name'] == 'lookup_project_record' and
            json.loads(calls[0]['arguments']) == dict(record_key='release'),
            'actual model tool request')
    call = calls[0]
    require(''.join(e['delta'] for e in events if
                    e['type'] == 'response.function_call_arguments.delta' and
                    e['response_id'] == rows[6]['id'] and e['item_id'] == call['id'])
            == call['arguments'], 'actual tool stream')
    require(any(e['type'] == 'response.output_text.delta' and
                e['response_id'] == rows[8]['id'] and e['delta'] for e in events),
            'cancellation must follow actual model output')
    updates = [e['session'] for e in events if e['type'] == 'session.updated']
    require(len(updates) == 4 and all(s['prompt']['version'] == '1' for s in updates)
            and all(s['max_output_tokens'] == 128 for s in updates),
            'session template pin/default/one-response isolation')
    require(all(not rows[i].get('conversation_id') for i in (0, 1, 2, 3, 4, 5, 8, 9)),
            'hidden prefix or out-of-band response entered public history')
    return total


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed implementation and verifier before freeze')
    build, cpu = args.build_record.resolve(strict=True), args.cpu_record.resolve(strict=True)
    require(build.is_relative_to(WORK) and cpu.is_relative_to(WORK), 'private stage receipts')
    result = json.loads((build / 'result.json').read_text())
    require(result['status'] == 'BUILD_PASS' and result['warnings'] == 0 and
            result['default_unchanged'], 'qualified runtime build')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == result['binary_sha256'], 'candidate binary drift')
    for name, digest in json.loads((build / 'plan.json').read_text())['sources'].items():
        require(sha(ROOT / name) == digest, 'built runtime source drift')
    require(json.loads((cpu / 'result.json').read_text())['status'] == 'PASS',
            'verifier CPU not qualified')
    previous = ROOT / '.q4t-work/serve-realtime-conversation-20261119/business-04/run'
    require(json.loads((previous / 'result.json').read_text())['status'] == 'PASS',
            'prior actual configuration not qualified')
    plan = json.loads((previous / 'plan.json').read_text())
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private stage output')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=1 if client['id'] == 'beta' else 20,
                      tokens_per_minute=60000, max_output_tokens=256)
    save(out / 'client-policy.json', policy); (out / 'client-policy.json').chmod(0o600)
    save(out / 'file-policy.json', dict(version=1, summary_output_tokens=256,
                                      total_summary_output_tokens=512))
    tls = certificate(out / 'credentials')
    paths = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    protected = [ROOT / p for p in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json',
        'build/service-native-state/ledger.json')]
    record = ROOT / 'docs/STATUS.md'
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'actual release source missing')
    csv_sources = []
    for label, folder in [('files', 'serve-realtime-prompt-files-cpu-20261120'),
                          ('runtime', 'serve-realtime-prompt-runtime-cpu-20261120')]:
        path = ROOT / 'docs/evidence' / folder / 'acceptance.json'
        value = json.loads(path.read_text())
        require(value['status'].startswith('PASS_'), 'actual CSV source not passed')
        csv_sources.append(dict(label=label, path=str(path), sha256=sha(path), tests=value['tests']))
    csv = out / 'acceptance-counts.csv'
    csv.write_text('stage,tests\n' + ''.join(f"{r['label']},{r['tests']}\n" for r in csv_sources))
    picture = ROOT / 'tests/fixtures/file_images/original.png'
    plan.update(schema='q4t-realtime-prompt-business-v1', limits=LIMITS, contract=CONTRACT,
        output=str(out), port=args.port, operations_port=args.port + 1,
        public_port=args.port + 2, binary=str(binary), binary_sha256=sha(binary),
        sources={n: sha(ROOT / n) for n in paths},
        protected={str(p): sha(p) for p in protected},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        file_policy_sha256=sha(out / 'file-policy.json'), reused_core_before={},
        qualified_files={str(p): sha(p) for folder in (build, cpu) for p in folder.iterdir() if p.is_file()},
        record=dict(path=str(record), sha256=sha(record), version='rev' + version.group(1)),
        csv_sources=csv_sources, csv_path=str(csv), csv_sha256=sha(csv),
        csv_sum=sum(r['tests'] for r in csv_sources),
        image=dict(path=str(picture), sha256=sha(picture), expected_left_color='red'),
        scope='Actual official SDK + native TLS Prompt/text/PNG/CSV/tools/cancel, scoped RPM/TPM; '
              'one model process, PID-bound admission fixture, no Guardian qualification or '
              'performance/full-model quality claim.')
    check_plan(plan); save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan); super().__init__(plan)
        for name, digest in plan['qualified_files'].items():
            require(sha(name) == digest, 'qualification changed')
        for row in [plan['record'], plan['image'], *plan['csv_sources']]:
            require(sha(row['path']) == row['sha256'], 'actual source changed')
        require(sha(plan['csv_path']) == plan['csv_sha256'] and
                sha(self.out / 'file-policy.json') == plan['file_policy_sha256'],
                'derived CSV or native file policy changed')

    def extra_model_args(self):
        return ['--allow-media', '--object-store-dir', str(self.out / 'objects'),
                '--file-input-policy-file', str(self.out / 'file-policy.json')]

    def budget(self, kind, amount=1):
        require(shutil.disk_usage(self.out).free >= 128 << 20, 'disk headroom')
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())
                <= 64 << 20, 'artifact budget')
        super().budget(kind, amount)

    def prompt(self, mode, foreign=None):
        self.budget('websocket_sessions'); self.budget('response_creates', len(CASES[mode]))
        self.budget('http_operations', 16)
        record, image = self.plan['record'], self.plan['image']
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys[mode], ca_file=self.plan['tls']['certificate'], mode=mode,
            record_file=record['path'], record_sha256=record['sha256'], expected_version=record['version'],
            csv_file=self.plan['csv_path'], csv_sha256=self.plan['csv_sha256'], csv_sum=self.plan['csv_sum'],
            image_file=image['path'], image_sha256=image['sha256'])
        if foreign: args.update(foreign_prompt=foreign['prompt'], foreign_file=foreign['file'])
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT / 'tools/serve_realtime/prompt_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=370)
        (self.out / (mode + '.sdk.json')).write_text(run.stdout)
        (self.out / (mode + '.stderr')).write_text(run.stderr)
        require(run.returncode == 0, 'actual Prompt SDK failed: ' + mode)
        require(len(run.stdout.encode()) <= 8 << 20, 'SDK receipt bound')
        result = json.loads(run.stdout)
        tokens = check_receipt(result, mode, record, self.plan['csv_sum'])
        self.same_model(); return result, tokens

    def business(self):
        self.snapshot('before'); self.core('core-before')
        alpha, tokens = self.prompt('alpha')
        settlement(self.snapshot('alpha-drained'), 'alpha', tokens, 10)
        _, tokens = self.prompt('beta', alpha['owned'])
        final = self.snapshot('beta-drained')
        settlement(final, 'beta', tokens, 1)
        require(final['q4t_client_rate_rejected_total{client="beta",kind="rpm"}'] == 1,
                'actual beta one-RPM denial')
        self.core('core-after'); self.snapshot('final-drained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--build-record', type=Path, default=WORK / 'engine-build-01')
    prep.add_argument('--cpu-record', type=Path, default=WORK / 'business-cpu-01')
    prep.add_argument('--port', type=int, default=18438)
    run = sub.add_parser('run'); run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with base.lifecycle.child_subreaper(): Stage(json.loads(args.plan.read_text())).run()
