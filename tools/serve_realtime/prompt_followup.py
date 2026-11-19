"""Finish interrupted Prompt checks; preserve the original wrong model answer."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import prompt_acceptance as original
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.history_acceptance import settlement
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

WORK = original.WORK
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              response_creates=5, chats=3, http_operations=16,
              controls=2000, seconds=900)
CASES = ['cancel-after-delta', 'new-request-after-cancel']
DIAGNOSTICS = [('seeded', 1, 0), ('greedy', 0, 0)]
CONTRACT = dict(original_quality='FAIL', original_result_unchanged=True,
    remaining_only=True, same_runtime=True, fixed_diagnostics=DIAGNOSTICS,
    automatic_replay=False, guardian_recovery=False, default_unchanged=True,
    master_complete=False)


def check_plan(plan):
    # JSON normalizes tuple values to arrays.
    require(plan['schema'] == 'q4t-realtime-prompt-followup-v1' and
            plan['limits'] == LIMITS and
            plan['contract'] == json.loads(json.dumps(CONTRACT)),
            'frozen continuation scope drift')


def check_prior(value, record, csv_sum):
    """Read only eight real responses. Never synthesize the unexecuted cases."""
    require(value['status'] == 'FAIL' and value['mode'] == 'alpha' and
            value['connections'] == 1 and value['automatic_replay'] is False and
            value['response_creates'] == 8 and value['http_calls'] == 7,
            'original failed SDK scope differs')
    rows, events = value['responses'], value['events']
    require(len(rows) == 8 and len({r['id'] for r in rows}) == 8 and
            [r['metadata']['acceptance_case'] for r in rows] == original.CASES['alpha'][:8] and
            [e['response'] for e in events if e['type'] == 'response.done'] == rows,
            'original real response identity or terminal changed')
    for row in rows:
        usage = row['usage']
        require(row['status'] == 'completed' and
                all(type(usage[k]) is int and usage[k] > 0 for k in
                    ('input_tokens', 'output_tokens', 'total_tokens')) and
                usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
                'original actual work or terminal invalid')
        require(original.text(row) == ''.join(e['delta'] for e in events if
                    e['type'] == 'response.output_text.delta' and e['response_id'] == row['id']),
                'original actual streamed text differs')
    for index, answer in enumerate(['42', '377', '72', record['version'], 'red', str(csv_sum)]):
        require(original.text(rows[index]).strip() == answer, 'passed prefix quality changed')
    source = dict(version=record['version'], source='docs/STATUS.md',
                  source_sha256=record['sha256'])
    returned = dict(record_key='release', **source)
    require(value['source_record'] == source and value['tool_record'] == returned,
            'original actual project source changed')
    calls = [i for i in rows[6]['output'] if i['type'] == 'function_call']
    require(len(calls) == 1 and calls[0]['status'] == 'completed' and
            calls[0]['name'] == 'lookup_project_record' and
            json.loads(calls[0]['arguments']) == dict(record_key='release'),
            'original actual model tool call missing')
    call = calls[0]
    require(''.join(e['delta'] for e in events if
                e['type'] == 'response.function_call_arguments.delta' and
                e['response_id'] == rows[6]['id'] and e['item_id'] == call['id'])
            == call['arguments'], 'original streamed tool arguments differ')
    completed = [i for i, e in enumerate(events) if e['type'] == 'conversation.item.done' and
        e['item'].get('type') == 'function_call_output' and
        e['item'].get('call_id') == call['call_id'] and
        json.loads(e['item']['output']) == returned]
    started = [i for i, e in enumerate(events) if e['type'] == 'response.created' and
               e['response']['id'] == rows[7]['id']]
    require(len(completed) == len(started) == 1 and completed[0] < started[0],
            'actual tool result must precede failed answer')
    answer = original.text(rows[7])
    require(answer.strip() != record['version'] and value['error'] ==
            'actual tool round trip quality: ' + json.dumps(answer),
            'original wrong answer must remain a quality failure')
    return sum(r['usage']['total_tokens'] for r in rows)


def check_remaining(value, prompt_id):
    require(value['status'] == 'PASS' and value['mode'] == 'remaining' and
            value['transport'] == 'official-openai-wss' and value['connections'] == 1 and
            value['close_code'] == 1000 and value['automatic_replay'] is False and
            value['response_creates'] == 2 and value['http_calls'] == 0,
            'continuation SDK boundary')
    rows, events = value['responses'], value['events']
    require(len(rows) == 2 and rows[0]['id'] != rows[1]['id'] and
            [r['metadata']['acceptance_case'] for r in rows] == CASES and
            [e['response'] for e in events if e['type'] == 'response.done'] == rows and
            [r['status'] for r in rows] == ['cancelled', 'completed'],
            'continuation real terminal missing, replayed or reordered')
    for row in rows:
        u = row['usage']
        require(all(type(u[k]) is int and u[k] > 0 for k in
                    ('input_tokens', 'output_tokens', 'total_tokens')) and
                u['total_tokens'] == u['input_tokens'] + u['output_tokens'] and
                not row.get('conversation_id'), 'actual usage or history isolation')
    require(any(e['type'] == 'response.output_text.delta' and
                e['response_id'] == rows[0]['id'] and e['delta'] for e in events),
            'cancel must follow an actual model delta')
    require(original.text(rows[1]).strip() == '56' and original.text(rows[1]) ==
            ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta' and
                    e['response_id'] == rows[1]['id']), 'new request actual quality and stream')
    updates = [e['session'] for e in events if e['type'] == 'session.updated']
    require(len(updates) == 2 and all(s['prompt']['id'] == prompt_id and
                s['prompt']['version'] == '1' for s in updates), 'retained Prompt isolation')
    return sum(r['usage']['total_tokens'] for r in rows)


def diagnostic_request(original_request, index):
    require(type(index) is int and 0 <= index < len(DIAGNOSTICS), 'fixed diagnostic only')
    require(original_request['temperature'] == 1 and original_request['top_p'] == 1 and
            'seed' not in original_request and original_request['stream'] is False,
            'original sampling contract drift')
    value = copy.deepcopy(original_request)
    _, value['temperature'], value['seed'] = DIAGNOSTICS[index]
    return value


def check_diagnostic(value, expected, input_tokens):
    require(len(value['choices']) == 1 and value['choices'][0]['finish_reason'] == 'stop',
            'diagnostic HTTP protocol incomplete')
    message, u = value['choices'][0]['message'], value['usage']
    require(all(type(u[k]) is int and u[k] > 0 for k in
                ('prompt_tokens', 'completion_tokens', 'total_tokens')) and
            u['prompt_tokens'] == input_tokens and
            u['total_tokens'] == u['prompt_tokens'] + u['completion_tokens'],
            'diagnostic actual input or usage changed')
    answer = message.get('content') or message.get('refusal')
    require(type(answer) is str and bool(answer.strip()) and not message.get('tool_calls'),
            'diagnostic final answer absent')
    return dict(answer=answer, expected=expected,
                quality='PASS' if answer.strip() == expected else 'FAIL',
                protocol='PASS', usage=u)


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed continuation before freezing')
    previous = WORK / 'business-02/run'
    old = json.loads((previous / 'plan.json').read_text())
    require(json.loads((previous / 'result.json').read_text())['status'] == 'FAIL' and
            json.loads((previous.parent / 'lease/exit.json').read_text())['status'] == 'COMMAND_FAILED',
            'original failed stage cannot be reclassified')
    receipt = json.loads((previous / 'alpha.sdk.json').read_text())
    check_prior(receipt, old['record'], old['csv_sum'])
    build = WORK / 'engine-build-01'
    require(sha(old['binary']) == old['binary_sha256'] ==
            json.loads((build / 'result.json').read_text())['binary_sha256'], 'same runtime required')
    for name, digest in json.loads((build / 'plan.json').read_text())['sources'].items():
        require(sha(ROOT / name) == digest, 'built production source drift')
    cpu = args.cpu_record.resolve(strict=True)
    require(cpu.is_relative_to(WORK) and
            json.loads((cpu / 'result.json').read_text())['status'] == 'PASS', 'CPU prerequisite')
    probe = WORK / 'answer-input-review-01'
    require(json.loads((probe / 'result.json').read_text())['status'] == 'PASS', 'input diagnosis')
    observed = json.loads((probe / 'observed.json').read_text())
    require(len(observed) == 2 and observed[0]['measured_prompt'] == observed[0]['executed_prompt'] ==
            observed[1]['measured_prompt'] == observed[1]['executed_prompt'] and
            observed[0]['tokens'] == observed[1]['tokens'] == receipt['responses'][7]['usage']['input_tokens'],
            'diagnostic actual prompt equality')
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private continuation directory')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    save(out / 'client-policy.json', policy); (out / 'client-policy.json').chmod(0o600)
    for source, target in [(previous / 'file-policy.json', 'file-policy.json'),
                           (previous / 'acceptance-counts.csv', 'acceptance-counts.csv'),
                           (probe / 'frozen-STATUS.md', 'frozen-STATUS.md')]:
        shutil.copy2(source, out / target)
    require(sha(out / 'frozen-STATUS.md') == old['record']['sha256'], 'original tool source')
    # A private copy preserves the original failed store and actual tenant IDs.
    shutil.copytree(previous / 'objects', out / 'objects')
    prior_files = [previous / n for n in ('plan.json', 'result.json', 'cleanup.json', 'alpha.sdk.json')]
    prior_files += [previous.parent / 'lease/exit.json', probe / 'observed.json', probe / 'result.json']
    prior_files += [p for p in (previous / 'objects').rglob('*') if p.is_file()]
    retained = {str(p): sha(p) for p in prior_files}
    frozen = {str(p): sha(p) for p in cpu.iterdir() if p.is_file()}
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    tls = certificate(out / 'credentials')
    old.update(schema='q4t-realtime-prompt-followup-v1', limits=LIMITS,
        contract=json.loads(json.dumps(CONTRACT)), output=str(out),
        port=args.port, operations_port=args.port + 1, public_port=args.port + 2,
        sources={n: sha(ROOT / n) for n in sources}, retained_files=retained,
        qualified_files=frozen, copied_objects={str(p.relative_to(out / 'objects')): sha(p)
            for p in (out / 'objects').rglob('*') if p.is_file()},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        record=dict(old['record'], path=str(out / 'frozen-STATUS.md')),
        csv_path=str(out / 'acceptance-counts.csv'), foreign=receipt['owned'],
        diagnostic=dict(original_request=observed[0]['generation_request'],
            input_tokens=observed[0]['tokens'], original_answer=original.text(receipt['responses'][7])),
        scope='Only unfinished cancellation/tenant/RPM cases and two fixed Chat diagnostics; '
              'original quality FAIL retained, no sampling loop, no Guardian qualification.')
    check_plan(old); save(out / 'plan.json', old)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(original.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan); base.Stage.__init__(self, plan)
        for name, digest in {**plan['retained_files'], **plan['qualified_files']}.items():
            require(sha(name) == digest, 'retained original or CPU evidence changed')
        for name, digest in plan['copied_objects'].items():
            require(sha(self.out / 'objects' / name) == digest, 'copied actual objects changed')
        require(sha(plan['record']['path']) == plan['record']['sha256'] and
                sha(plan['csv_path']) == plan['csv_sha256'] and
                sha(self.out / 'file-policy.json') == plan['file_policy_sha256'],
                'actual source or policy changed')

    def remaining(self):
        self.budget('websocket_sessions'); self.budget('response_creates', 2)
        args = dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'], ca_file=self.plan['tls']['certificate'], mode='remaining',
            csv_file=self.plan['csv_path'], csv_sha256=self.plan['csv_sha256'],
            foreign_prompt=self.plan['foreign']['prompt'])
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        run = subprocess.run(['node', str(ROOT / 'tools/serve_realtime/prompt_sdk.mjs')],
            input=json.dumps(args), text=True, capture_output=True, env=env, timeout=370)
        (self.out / 'remaining.sdk.json').write_text(run.stdout)
        (self.out / 'remaining.stderr').write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= 8 << 20, 'remaining SDK failed')
        result = json.loads(run.stdout)
        tokens = check_remaining(result, self.plan['foreign']['prompt'])
        self.same_model(); return tokens

    def diagnostic(self, index):
        self.budget('chats')
        label = DIAGNOSTICS[index][0]
        request = diagnostic_request(self.plan['diagnostic']['original_request'], index)
        save(self.out / (label + '.request.json'), request)
        conn = http.client.HTTPSConnection('127.0.0.1', self.plan['public_port'], timeout=120,
            context=ssl.create_default_context(cafile=self.plan['tls']['certificate']))
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(request), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.keys['gamma']})
            response = conn.getresponse(); raw = response.read(1048577)
            (self.out / (label + '.wire')).write_bytes(raw)
            require(response.status == 200 and len(raw) <= 1048576, 'diagnostic HTTP status/bound')
            outcome = check_diagnostic(json.loads(raw), self.plan['record']['version'],
                                       self.plan['diagnostic']['input_tokens'])
            save(self.out / (label + '.diagnosis.json'), outcome)
        finally:
            conn.close()
        self.same_model(); return outcome

    def business(self):
        self.snapshot('before')
        diagnoses = [self.diagnostic(i) for i in range(len(DIAGNOSTICS))]
        tokens = self.remaining()
        settlement(self.snapshot('alpha-drained'), 'alpha', tokens, 2)
        _, tokens = self.prompt('beta', self.plan['foreign'])
        final = self.snapshot('beta-drained')
        settlement(final, 'beta', tokens, 1)
        require(final['q4t_client_rate_rejected_total{client="beta",kind="rpm"}'] == 1,
                'actual single RPM rejection')
        self.core('core-after')
        core = json.loads((self.out / 'core-after.wire').read_text())
        final = self.snapshot('final-drained')
        settlement(final, 'gamma', core['usage']['total_tokens'] +
                   sum(v['usage']['total_tokens'] for v in diagnoses), 3)
        save(self.out / 'business.json', dict(status='PASS_REMAINING_CHECKS',
            original_quality='FAIL', original_answer=self.plan['diagnostic']['original_answer'],
            diagnoses=diagnoses, original_stage_reclassified=False))

    def cleanup(self):
        super().cleanup()
        for name, digest in self.plan['retained_files'].items():
            require(sha(name) == digest, 'original failed evidence changed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--cpu-record', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18438)
    run = sub.add_parser('run'); run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with base.lifecycle.child_subreaper(): Stage(json.loads(args.plan.read_text())).run()
