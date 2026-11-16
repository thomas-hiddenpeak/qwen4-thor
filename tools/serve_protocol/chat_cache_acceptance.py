"""One owned model/eight Chat requests: actual SSD cache boundaries and isolation."""
import argparse
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read, require, save, sha
from chat_client import read_json, read_sse

LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)
OTHER_CLIENT = 'chat-cache-other'


def requests():
    archive = (ROOT / foreground.DOCUMENT).read_text()[:4200]
    record = ('The primary project code is 42. The secondary project code is 43.\n'
              'These explicit codes override numbers in the reference archive.\n')
    parts = [dict(type='text', text='Project reference archive:\n' + archive,
                  prompt_cache_breakpoint=dict(mode='explicit', ttl='12h')),
             dict(type='text', text='\nCurrent record:\n' + record,
                  prompt_cache_breakpoint=dict(mode='explicit', ttl='30m')),
             dict(type='text', text='\nReturn only the primary project code.')]
    common = dict(model=foreground.MODEL, messages=[dict(role='user', content=parts)],
                  reasoning_effort='none', temperature=0, max_completion_tokens=64,
                  prompt_cache_key='chat-cache:primary',
                  prompt_cache_options=dict(mode='explicit', ttl='30m'))
    rows = {label: copy.deepcopy(common) for label in (
        'write', 'hit', 'client_isolated', 'key_isolated', 'disabled',
        'default_implicit', 'target_write', 'target_hit')}
    rows['hit']['messages'][0]['content'][-1]['text'] = (
        '\nReturn only the secondary project code.')
    rows['key_isolated']['prompt_cache_key'] = 'chat-cache:other-key'
    for label in ('disabled', 'default_implicit'):
        for part in rows[label]['messages'][0]['content']:
            part.pop('prompt_cache_breakpoint', None)
    rows['default_implicit'].pop('prompt_cache_options')
    rows['default_implicit'].pop('prompt_cache_key')
    for label in ('target_write', 'target_hit'):
        rows[label]['response_format'] = dict(type='json_schema', json_schema=dict(
            name='project_code', strict=True, schema=foreground.base.closed(
                dict(answer=dict(type='integer', const=42)))))
        rows[label]['messages'][0]['content'][-1]['text'] = (
            '\nReturn the primary project code as {"answer":the code}.')
    for label in ('hit', 'target_hit'):
        rows[label].update(stream=True, stream_options=dict(
            include_usage=True, include_obfuscation=False))
    for label, row in rows.items():
        row.update(request_id='chat-cache-' + label,
                   cancel_token=hashlib.sha256(label.encode()).hexdigest())
    return rows


def check_answer(value, label):
    expected = '43' if label == 'hit' else '42'
    content = value.message['content'].strip()
    require(value.finish_reason == 'stop' and
            (json.loads(content) == dict(answer=42) if label.startswith('target_')
             else content == expected), 'actual cached answer: ' + label)
    usage = value.usage
    cached = usage['prompt_tokens_details']['cached_tokens']
    require(usage['prompt_tokens'] >= 1024 and usage['completion_tokens'] > 0 and
            usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'],
            'actual Chat cache usage')
    hit = label in ('hit', 'target_hit')
    require((1024 <= cached < usage['prompt_tokens']) if hit else cached == 0,
            'actual hit/miss differs: ' + label)


def check_cache_logs(log, values):
    proof = {}
    for label, value in values.items():
        identity = re.escape(value.id)
        writes = re.findall(r'\[prefix-cache\] request=' + identity +
            r' event=write prefix=(\d+) ttl=(\d+) saved=(\d+) bytes=(\d+)', log)
        restores = re.findall(r'\[session-cache\] request=' + identity +
            r' event=restore prefix=(\d+) ms=\S+ error=(\S+) source=(\w+) layout=(\w+)', log)
        saved = [int(row[0]) for row in writes]
        require(saved == sorted(set(saved)) and
                all(int(row[0]) >= 1024 and row[2] == '1' and int(row[3]) > 0
                    for row in writes), 'actual SSD persistence differs: ' + label)
        if label in ('hit', 'target_hit'):
            layout = 'target' if label.startswith('target_') else 'mtp'
            require(len(restores) == 1 and restores[0] == (
                str(value.usage['prompt_tokens_details']['cached_tokens']),
                'none', 'shared', layout), 'usage unsupported by actual restore')
            require(not writes, 'hit unexpectedly rewrote selected states')
        else:
            require(not any(row[2] == 'shared' and int(row[0]) > 0 for row in restores),
                    'isolation/no-marker request restored state')
            expected = [] if label == 'disabled' else (
                [1800] if label == 'default_implicit' else [43200, 1800])
            require([int(row[1]) for row in writes] == expected,
                    'selected breakpoint count or TTL differs: ' + label)
        proof[label] = dict(writes=writes, restores=restores)
    require('[mtp-accept] request=' + values['hit'].id in log,
            'cached MTP path was not executed')
    return proof


def prepare(args):
    foreground.base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    for name in subprocess.check_output(
            ['git', 'ls-files', 'tools/serve_responses'], cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT / name)
    plan['planned'] = dict(starts=1, chats=8, clients=2, actual_ssd_writes=True,
                           actual_mtp_and_target_restores=True)
    plan['mutations'] = ['one own model; private two-client state/cache only',
        'no corruption/restart/other-service control; no accepted default change']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_CHAT_CACHE_BUSINESS'
    requests_per_minute = 16
    tokens_per_minute = 65536
    active_client = encrypted.CLIENT

    def client_policy(self):
        policy = super().client_policy()
        policy['clients'].append(dict(policy['clients'][0], id=OTHER_CLIENT))
        return policy

    def http(self, path, body=None, *, label=None, timeout=30):
        if body is None:
            self.budget('controls')
        headers = {} if path in ('/readyz', '/metrics', '/v1/config') else dict(
            encrypted.HEADERS, **{'X-Q4T-Client-Id': self.active_client})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        started = time.monotonic()
        try:
            conn.request('GET' if body is None else 'POST', path,
                body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                headers=headers)
            response = conn.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, 'response size bound')
            if label:
                (self.out / (label + '.wire')).write_bytes(raw)
                save(self.out / (label + '.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), seconds=time.monotonic()-started,
                    client=self.active_client))
            return response.status, raw
        finally:
            conn.close()

    def business(self):
        caps = read(self.out / 'capabilities-1.json')['chat']['prompt_cache']
        require(caps['default_mode'] == 'implicit' and caps['latest_lookup_boundaries'] == 80
                and caps['client_key_isolation'] and not caps['core_readiness_dependency'],
                'new Chat cache capability absent')
        values = {}
        for label, row in self.plan['requests'].items():
            self.active_client = OTHER_CLIENT if label == 'client_isolated' else encrypted.CLIENT
            self.budget('controls')
            status, raw = self.http('/v1/chat/preflight', dict(
                request=row, include_prompt=True), label=label+'-preflight')
            require(status == 200, 'actual preflight failed: ' + label)
            require('prompt_cache_breakpoint' not in json.loads(raw)['prompt'],
                    'cache metadata entered prompt')
            self.budget('chats')
            save(self.out / (label + '.request.json'), row)
            status, raw = self.http('/v1/chat/completions', row, label=label, timeout=120)
            require(status == 200, 'actual Chat failed: ' + label)
            value = (read_sse if row.get('stream') else read_json)(io.BytesIO(raw))
            check_answer(value, label)
            values[label] = value
            self.same_model()
            self.drained(label + '-drain')
        metrics = self.drained('final')
        inputs = sum(v.usage['prompt_tokens'] for v in values.values())
        outputs = sum(v.usage['completion_tokens'] for v in values.values())
        require(metrics['q4t_requests_total'] == 8 and
                metrics['q4t_requests_success_total'] == 8 and
                metrics['q4t_requests_error_total'] == 0 and
                metrics['q4t_prompt_tokens_total'] == inputs and
                metrics['q4t_generation_tokens_total'] == outputs, 'actual terminal accounting')
        for client in (encrypted.CLIENT, OTHER_CLIENT):
            selected = [v for label, v in values.items() if
                        (label == 'client_isolated') == (client == OTHER_CLIENT)]
            key = '{client="' + client + '"}'
            require(metrics['q4t_client_rate_admitted_total' + key] == len(selected) and
                    metrics['q4t_client_rate_reserved_tokens' + key] == 0 and
                    metrics['q4t_client_rate_settled_tokens_total' + key] ==
                    sum(v.usage['total_tokens'] for v in selected), 'per-client settlement')
        proof = check_cache_logs((self.out / 'model-1.log').read_text(), values)
        save(self.out / 'business-checks.json', dict(actual_requests=8,
            clients=2, key_and_client_isolation=True, default_implicit_write=True,
            explicit_no_markers_disabled=True, exact_restore_usage=True,
            actual_prefix_writes_and_restores=proof, actual_answers=True,
            full_performance_claim=False, media_cache_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18772)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
