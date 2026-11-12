"""Revalidate only the corrected unknown-commit exit and same-model continuation."""
import argparse
import json
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_chat_storage import acceptance as stage
from tools.serve_chat_storage import remainder_acceptance as prior
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=2, controls=1000, seconds=300)
CORRECTED = {'include/q4t/server/object_store.h', 'src/server/chat_generation.cpp',
             'src/server/chat_http.cpp', 'src/server/responses_api.cpp'}


def accepted_tool_prefix(previous):
    plan, result = read(previous/'plan.json'), read(previous/'result.json')
    require(result['status'] == 'FAILED' and result['counts']['chats'] == 3 and
            result['counts']['starts'] == 1 and result['seconds'] < 300,
            'unexpected second attempt')
    cleanup = read(previous/'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and cleanup['ports_released'],
            'second attempt cleanup unconfirmed')
    original, _ = prior.prefix(Path(plan['previous']))
    tool = read(previous/'tool-stored.sdk.json')['result']
    stage.shape(tool, 'stored_chat')
    stage.check_stream(read(previous/'tool.sdk.json')['chunks'], tool)
    actual = read(previous/'tool-execution.json')
    require(actual['call'] == tool['choices'][0]['message']['tool_calls'][0] and
            actual['result']['source']['sha256'] == plan['sources'][stage.DOCUMENT], 'tool source')
    require(json.loads(actual['call']['function']['arguments']) ==
            dict(path=stage.DOCUMENT, start_line=1, line_count=3), 'tool arguments')
    final = read(previous/'follow.sdk.json')['result']
    stage.shape(final, 'stored_chat')
    answer = json.loads(final['choices'][0]['message']['content'])
    title = actual['result']['source']['text'].splitlines()[0].split(': ', 1)[1]
    require(answer['title'] in (title, title.lstrip('# ')) and
            answer['citation'] == actual['result']['source']['citation'], 'real tool answer')
    history = read(previous/'stored-history.sdk.json')['result']
    stage.shape(history, 'stored_chat_messages')
    for stored, requested in zip(history['data'], read(previous/'follow.request.json')['request']['messages'], strict=True):
        require(all(stored.get(k) == v for k, v in requested.items()), 'stored original messages')
    updated = read(previous/'metadata-update.sdk.json')['result']
    require(updated == dict(original, metadata=None), 'metadata changed actual output')
    # Preserve the discovered failure as a failure, not accepted semantics.
    errors = [e['error'] for e in read(previous/'fault.events.json') if 'error' in e]
    require(len(errors) == 1 and errors[0]['code'] == 'object_timeout', 'wrong known defect')
    return updated


def prepare(args):
    previous = args.previous.resolve(strict=True)
    accepted_tool_prefix(previous)
    old = read(previous/'plan.json')
    changed = {name for name, digest in old['sources'].items() if sha(ROOT/name) != digest}
    require(changed == CORRECTED, 'unexpected source delta: '+str(changed))
    stage.background.prepare(args, rows=stage.requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_chat_storage',
        'docs/serve-transport-v1.schema.json'], cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT/name)
    prior_hashes = {}
    for file in previous.rglob('*'):
        mode = file.lstat().st_mode
        require(not stat.S_ISLNK(mode), 'prior evidence symlink')
        if stat.S_ISREG(mode):
            prior_hashes[str(file.relative_to(previous))] = sha(file)
    plan.update(previous=str(previous), previous_sha256=prior_hashes,
        corrected_error_sources=sorted(changed),
        cumulative_limits=dict(starts=3, chats=8, choices=10, seconds=600),
        mutations=['private copy of prior accepted state', 'SIGSTOP exact own object worker after real text',
                   'native worker recovery, no model restart'],
        derived_requests=['repeat only corrected final-storage failure and same-model Chat',
                          'reuse actual completed normal/tool generation from earlier attempts',
                          'confirm earlier object retained across process and worker recovery; delete/query'])
    save(path, plan)


class Stage(stage.Stage):
    limits = LIMITS
    success_status = 'PASS_STORED_CHAT_CORRECTED_FAULT_AND_RECOVERY'

    def start(self):
        previous = Path(self.plan['previous'])
        for name, digest in self.plan['previous_sha256'].items():
            require(sha(previous/name) == digest, 'prior original changed')
        self.saved = accepted_tool_prefix(previous)
        source, target = previous/'objects', self.out/'objects'
        require(not target.exists(), 'target state already exists')
        target.mkdir(mode=0o700)
        for path in source.iterdir():
            require(stat.S_ISREG(path.lstat().st_mode), 'nonregular old state')
            if path.name == '.q4t-lock':
                continue
            shutil.copyfile(path, target/path.name)
            (target/path.name).chmod(0o600)
            require(sha(path) == sha(target/path.name), 'copied state drift')
        super().start()

    def business(self):
        require(self.sdk_chat('initial-retained', 'retrieve', identity=self.saved['id']) == self.saved,
                'prior saved object lost')
        self.fault()
        answer = self.sdk_chat('same-pid-chat', 'create', self.plan['requests']['chat'])
        require(answer['choices'][0]['finish_reason'] == 'stop' and
                answer['choices'][0]['message']['content'].strip() == '42', 'same-model healthy business')
        require('[mtp-accept] request='+answer['id'] in (self.out/'model-1.log').read_text(),
                'same-model continuation did not exercise MTP')
        require(self.sdk_chat('retained-after-worker-recovery', 'retrieve', identity=self.saved['id']) == self.saved,
                'previous durable data lost')
        self.sdk_chat('delete', 'delete', identity=self.saved['id'])
        self.sdk_chat('deleted-gone', 'retrieve', identity=self.saved['id'], expected_status=404)
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 2 and metrics['q4t_requests_success_total'] == 1 and
                metrics['q4t_requests_error_total'] == 1, 'corrected failure accounting')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'config unavailable')
        save(self.out/'final-config.json', json.loads(raw))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--previous', type=Path, required=True)
    p.add_argument('--port', type=int, default=18164)
    p = sub.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_signal, _frame):
            raise TimeoutError('stored Chat corrected-fault deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with stage.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
