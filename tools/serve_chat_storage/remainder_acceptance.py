"""Continue only unexecuted stored Chat business, preserving the failed attempt."""
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
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=4, controls=1000, seconds=300)
CHANGED_DRIVER = {'tools/serve_chat_storage/acceptance.py',
                  'tools/serve_chat_storage/sdk.mjs',
                  'tools/serve_chat_storage/test_bridge.mjs'}


def prefix(previous):
    """Revalidate actual accepted prefix from immutable records, without GPU."""
    result = read(previous/'result.json')
    require(result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
            result['counts']['chats'] == 3 and result['seconds'] < 300,
            'not the known 3-request failed attempt')
    cleanup = read(previous/'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
            cleanup['ports_released'], 'first attempt cleanup unconfirmed')
    stored = read(previous/'json.sdk.json')['result']
    stream = read(previous/'stream-stored.sdk.json')['result']
    stage.shape(stored, 'stored_chat')
    stage.shape(stream, 'stored_chat')
    require(stored == read(previous/'json-retrieve.sdk.json')['result'], 'JSON prefix mismatch')
    require(len(stored['choices']) == len(stream['choices']) == 2 and
            all(c['finish_reason'] == 'stop' and c['message']['content'] == '42'
                for value in (stored, stream) for c in value['choices']), 'prefix answer')
    stage.check_stream(read(previous/'stream.sdk.json')['chunks'], stream)
    plain = read(previous/'unretained.sdk.json')['result']
    require(plain['choices'][0]['message']['content'] == '42' and
            plain['metadata'] == dict(stage='response-only') and
            read(previous/'unretained-gone.sdk.json')['error']['status'] == 404, 'unretained prefix')
    # The error was only the acceptance bridge's projection; actual HTTP is valid.
    page = json.loads((previous/'filtered-list.wire').read_bytes())
    stage.shape(page, 'stored_chat_list')
    require(page['data'] == [stream] and not page['has_more'], 'raw filtered page')
    return stored, stream


def prepare(args):
    previous = args.previous.resolve(strict=True)
    prefix(previous)
    old = read(previous/'plan.json')
    changed = {name for name, digest in old['sources'].items()
               if sha(stage.ROOT/name) != digest}
    require(changed == CHANGED_DRIVER, 'unexpected source drift after prefix: '+str(changed))
    require(sha(args.binary) == old['binary_sha256'], 'engine changed after accepted prefix')
    stage.background.prepare(args, rows=stage.requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_chat_storage',
        'docs/serve-transport-v1.schema.json'], cwd=stage.ROOT, text=True).splitlines():
        plan['sources'][name] = sha(stage.ROOT/name)
    prior = {}
    for file in previous.rglob('*'):
        mode = file.lstat().st_mode
        require(not stat.S_ISLNK(mode), 'prior evidence symlink')
        if stat.S_ISREG(mode):
            prior[str(file.relative_to(previous))] = sha(file)
    plan.update(previous=str(previous), previous_sha256=prior,
        source_delta=sorted(changed), cumulative_limits=dict(starts=2, chats=7, seconds=600),
        maximum_remaining_choices=4,
        mutations=['copy verified prior persistent objects to this private stage',
                   'SIGSTOP exact own object worker; model never restarted'],
        derived_requests=['reuse original successful 3 Chat requests; no generation replay',
            'remaining filtered query, tool and follow-up, fault, same-PID Chat, original object CRUD'])
    save(path, plan)


class Stage(stage.Stage):
    limits = LIMITS
    success_status = 'PASS_STORED_CHAT_REMAINDER'

    def start(self):
        previous = Path(self.plan['previous'])
        for name, digest in self.plan['previous_sha256'].items():
            require(sha(previous/name) == digest, 'prior original changed')
        self.stored, self.stream = prefix(previous)
        source, target = previous/'objects', self.out/'objects'
        require(not target.exists(), 'remainder object directory already exists')
        target.mkdir(mode=0o700)
        for path in source.iterdir():
            require(stat.S_ISREG(path.lstat().st_mode), 'only regular saved objects may be copied')
            if path.name == '.q4t-lock':
                continue
            shutil.copyfile(path, target/path.name)
            (target/path.name).chmod(0o600)
            require(sha(path) == sha(target/path.name), 'private state copy drift')
        super().start()

    def business(self):
        self.remainder(self.stored, self.stream, 4)


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
            raise TimeoutError('stored Chat remainder deadline')
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
