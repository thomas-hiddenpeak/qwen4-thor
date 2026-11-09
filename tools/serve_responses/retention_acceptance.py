"""A08 actual SDK state/retention closure: <=2 starts/4 generations/900 seconds."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=4, controls=2000, seconds=900)


def requests():
    common = dict(encrypted.foreground.requests()['plain'], store=True,
                  reasoning=dict(effort='low'), max_output_tokens=384,
                  include=['reasoning.encrypted_content'])
    continuation = dict(common, input='What was the result of the preceding '
        'calculation? Reply only with the decimal number.', stream=True,
        stream_options=dict(include_obfuscation=False))
    return dict(initial=common, history=continuation,
                background=dict(common, background=True, stream=True,
                    stream_options=dict(include_obfuscation=False)),
                encrypted=dict(continuation, store=False))


def check_answer(value):
    encrypted.encrypted_items(value)
    require(encrypted.foreground.text(value).strip() == '42', 'actual arithmetic/history failed')


def check_plan(receipt, *, applied=False):
    require(receipt['status'] == 200 and not receipt['commit_unknown'],
            'retention operation not confirmed')
    value = receipt['value']
    require(value['object'] == 'q4t.state_retention' and
            value['applied'] is applied and
            re.fullmatch('[0-9a-f]{64}', value['plan_sha256']), 'invalid retention identity')
    require(len(value['candidates']) <= value['limit'] <= 128 and
            len({c['owner_sha256'] for c in value['candidates']}) <= 1,
            'retention batch bounds')
    for row in value['candidates']:
        require(set(row) == {'owner_sha256', 'kind', 'id', 'revision',
                             'created_at', 'expires_at', 'bytes'},
                'retention receipt leaks payload or misses identity')
    require(value['deleted_objects'] == (len(value['candidates']) if applied else 0),
            'retention application count')
    return value


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/retention_acceptance.py'] = sha(Path(__file__))
    plan['planned'] = dict(starts=2, chats=4, model_steps=4)
    plan['mutations'] = ['only private SDK conversations/responses and state-retention '
        'collect using actual exact plan; no editing or deleting object files',
        'graceful owned model stop then one explicit restart on same private store']
    plan['derived_requests'] = [
        'conversation/response/item IDs and plan digest from actual SDK/CLI receipts',
        'explicit cutoff is wall clock after object creation, not an invented orphan age',
        'collect stale plan returns409; remove one unreferenced item while history remains usable',
        'stored background JSON/SSE replay and ciphertext continue after native restart',
        'all four generation usage and model steps exact; no automatic generation replay']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_APPLICATION_RETENTION_BUSINESS'

    def retention(self, label, cutoff=0, plan=None, *, expected_status=200, offline=False):
        self.budget('controls')
        if not offline:
            self.same_model()
        command = [self.plan['binary'], 'state-retention',
            'collect' if plan else 'inspect', '--object-store-dir', str(self.out/'objects'),
            '--limit', '128']
        if cutoff:
            command += ['--item-created-before', str(cutoff)]
        if plan:
            command += ['--expected-plan', plan]
        if offline:
            command += ['--offline']
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        save(self.out/(label+'.cli.json'), dict(command=command,
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
        receipt = json.loads(result.stdout)
        require(not result.stderr and receipt['status'] == expected_status and
                result.returncode == (0 if expected_status == 200 else 1),
                'retention CLI status: ' + label)
        if not offline:
            self.same_model()
        if expected_status != 200:
            require(receipt['code'] == 'retention_plan_changed' and
                    not receipt['commit_unknown'], 'stale plan did not fail before mutation')
            return receipt
        return check_plan(receipt, applied=bool(plan))

    def business(self):
        rows = self.plan['requests']
        conversation = self.sdk('conversation', 'conversation_create', dict(items=[
            dict(role='user', content='Remember that the project validation result is 42.')]))
        first = self.sdk('initial', 'create', dict(rows['initial'], conversation=conversation['id']))
        check_answer(first)
        items = self.sdk('conversation-items', 'conversation_items',
                         response_id=conversation['id'])
        protected_ids = {item['id'] for item in items['data']}
        require(protected_ids, 'conversation has no actual stored items')
        orphan = self.sdk('orphan-conversation', 'conversation_create', dict(items=[
            dict(role='user', content='Temporary project note for explicit retention cleanup.')]))
        orphan_items = self.sdk('orphan-items', 'conversation_items', response_id=orphan['id'])
        orphan_id = orphan_items['data'][0]['id']
        until = time.monotonic() + 2
        while int(time.time()) <= max(orphan['created_at'], first['created_at']):
            require(time.monotonic() < until, 'wall clock did not pass creation cutoff')
            time.sleep(.05)
        cutoff = int(time.time())
        before = self.retention('before-delete', cutoff)
        require(before['eligible_objects'] == 0, 'referenced items selected')
        self.sdk('delete-orphan-conversation', 'conversation_delete', response_id=orphan['id'])
        self.retention('stale-plan', cutoff, before['plan_sha256'], expected_status=409)
        proposed = self.retention('proposed', cutoff)
        require(proposed['eligible_objects'] == 1 and
                {row['id'] for row in proposed['candidates']} == {orphan_id},
                'cleanup must select only actual orphan item')
        applied = self.retention('collected', cutoff, proposed['plan_sha256'])
        require(applied['deleted_objects'] == 1, 'orphan not collected')
        require(self.retention('after-collect', cutoff)['eligible_objects'] == 0,
                'unexpected remaining orphan')
        retained = self.sdk('retained-items', 'conversation_items', response_id=conversation['id'])
        require({item['id'] for item in retained['data']} == protected_ids,
                'collection altered retained conversation')
        second = self.sdk('history', 'create', dict(rows['history'], previous_response_id=first['id']))
        check_answer(second)
        stored = self.sdk('background', 'create', rows['background'])
        check_answer(stored)
        self.accounting('first-accounting', [first, second, stored])
        key_state = self.keys('before-restart-keys')
        self.cleanup()
        cleanup = read(self.out/'cleanup.json')
        save(self.out/'first-cleanup.json', cleanup)
        require(cleanup['model_exit'] == 0, 'owned first model did not stop gracefully')
        self.model = None
        offline = self.retention('offline-inspect', cutoff, offline=True)
        require(offline['eligible_objects'] == 0, 'restart lost retained roots')
        self.start()
        require(self.keys('after-restart-keys') == key_state, 'retention changed key ring')
        replay = self.sdk('background-replay', 'retrieve', response_id=stored['id'],
            query=dict(stream=True, include_obfuscation=False,
                       include=['reasoning.encrypted_content']))
        require(replay == stored, 'stored background events changed across retention/restart')
        fetched = self.sdk('first-retrieve', 'retrieve', response_id=first['id'],
            query=dict(include=['reasoning.encrypted_content']))
        require({k: v for k, v in fetched.items() if k != 'output_text'} == first,
                'durable previous response changed')
        last = self.sdk('encrypted-continuation', 'create',
                        encrypted.continuation(rows['encrypted'], stored))
        check_answer(last)
        self.accounting('second-accounting', [last])
        logs = ''.join((self.out/f'model-{i}.log').read_text() for i in (1, 2))
        for value in (first, second, stored, last):
            summary.check_steps(logs, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', logs, re.M)) == 4,
                'retention/replay performed extra model work')
        save(self.out/'business-checks.json', dict(generations=4, model_steps=4,
            actual_sdk_conversation=True, stale_plan_conflict=True,
            explicit_orphan_deleted=orphan_id, retained_items=sorted(protected_ids),
            live_admin_same_model=True, offline_exclusive=True,
            history_and_ciphertext_survive_restart=True, original_background_sse=True,
            exact_per_process_usage=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=19003)
    command = sub.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with encrypted.foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
