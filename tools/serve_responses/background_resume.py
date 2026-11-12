"""Finish only unexecuted background checks: 2 starts/3 creates/600 seconds."""
import argparse
import json
from pathlib import Path
import shutil
import signal
import stat
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as prior
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=3, controls=2000, seconds=600)


def requests():
    old = prior.requests()
    return {name: old[name] for name in ('crash_running', 'crash_queued', 'chat')}


def inventory(root):
    """Freeze a small closed store; never follow symlinks or copy special files."""
    require(stat.S_ISDIR(root.lstat().st_mode), 'store root must be a real directory')
    result, total = {}, 0
    for path in sorted(root.iterdir()):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode), 'store must contain only regular files')
        total += info.st_size
        require(len(result) < 1000 and total <= 8*1024*1024, 'store copy bound')
        result[path.name] = sha(path)
    require('.q4t-format' in result and '.q4t-lock' in result, 'not a native store')
    return result


def review(original, lease):
    plan = read(original/'plan.json')
    result = read(original/'result.json')
    cleanup = read(original/'cleanup.json')
    outcome = read(lease/'exit.json')
    require(Path(plan['output']) == original and plan['limits'] == prior.LIMITS,
            'original phase identity')
    require(result['status'] == 'FAILED' and 'cancel changed already-terminal resource' in
            result['error'] and result['counts'] == dict(starts=1, chats=5, controls=146),
            'not the specific reviewed oracle failure')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'original cleanup unconfirmed')
    records = prior.base.lifecycle.guard.processes()
    require(all(records.get(int(pid), {}).get('start_ticks') != identity['start_ticks']
                for pid, identity in cleanup['owned'].items()), 'old process still present')
    require(outcome['status'] == 'COMMAND_FAILED' and outcome['child_returncode'] == 1 and
            outcome['lease_inherited_by_child'] and
            Path(outcome['command'][-1]).resolve() == original/'plan.json', 'original lease identity')
    events = [json.loads(line) for line in (lease/'events.jsonl').read_text().splitlines()]
    queries = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    require(events[0]['event'] == 'lease_acquired' and events[-1]['event'] == 'lease_released' and
            all(not e.get('conflicts') and not e.get('unreadable_processes') for e in events) and
            queries and all(q['status'] == 'OK' and not q['observed_errors'] for q in queries),
            'original strict lease did not remain valid')
    # Only the reviewed acceptance oracle may differ. Runtime and all shared
    # validation/SDK dependencies must be the exact accepted identity.
    changed = {'tools/serve_responses/background_acceptance.py',
               'tools/serve_responses/test_background_acceptance.py'}
    for name, digest in plan['sources'].items():
        if name not in changed:
            require(sha(ROOT/name) == digest, 'reused evidence source drift: '+name)
    require(sha(plan['binary']) == plan['binary_sha256'] == result['binary_sha256'],
            'reused engine identity changed')
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default changed')
    first = read(original/'running-cancel.sdk.json')['result']
    again = read(original/'running-cancel-idempotent.sdk.json')['result']
    projected = read(original/'running-cancelled.poll.json')[-1]
    prior.check_cancel(first, again, projected)
    require(first['output'] and first['store'], 'cancelled durable partial output absent')
    files = {str(p): sha(p) for p in original.iterdir() if p.is_file()}
    files.update({str(p): sha(p) for p in lease.iterdir() if p.is_file()})
    return dict(original=str(original), lease=str(lease), files=files,
        objects=inventory(original/'objects'), cancelled_id=first['id'],
        binary_sha256=plan['binary_sha256'], reused_counts=result['counts'],
        reused_seconds=result['seconds'], original_result='FAILED_ORACLE_PROJECTION',
        completed_generations=['plain', 'tool', 'follow', 'cancel_running', 'cancel_queued'],
        cumulative_limits=dict(starts=3, chats=8, seconds=result['seconds']+LIMITS['seconds']))


def prepare(args):
    original, lease = args.original.resolve(strict=True), args.lease.resolve(strict=True)
    require(original.is_relative_to(ROOT/'.q4t-work') and lease.is_relative_to(ROOT/'.q4t-work'),
            'reuse only workspace evidence')
    reuse = review(original, lease)
    require(sha(args.binary) == reuse['binary_sha256'], 'continuation changed engine')
    prior.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['reuse'] = reuse
    target = args.output.resolve()/'objects'
    target.mkdir(mode=0o700)
    for name, digest in reuse['objects'].items():
        source = original/'objects'/name
        require(sha(source) == digest, 'closed original store changed')
        shutil.copyfile(source, target/name, follow_symlinks=False)
        (target/name).chmod(0o600)
    require(inventory(target) == reuse['objects'], 'store snapshot differs')
    plan['mutations'] = ['reuse a private copy of the closed prior store; original remains untouched',
        'SIGKILL exact own first model, clean owned children, then one explicit restart']
    save(path, plan)


class Stage(prior.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_BACKGROUND_REMAINDER'

    def __init__(self, plan):
        super().__init__(plan)
        reuse = plan['reuse']
        require(review(Path(reuse['original']), Path(reuse['lease'])) == reuse, 'reuse drift')
        require(inventory(self.out/'objects') == reuse['objects'], 'private snapshot drift')

    def business(self):
        identity = self.plan['reuse']['cancelled_id']
        cancelled = self.sdk('preserved-cancelled', 'retrieve', response_id=identity)
        prior.check_response(cancelled, 'cancelled')
        self.sdk('cancelled-replay', 'retrieve', response_id=identity,
            query=dict(stream=True, include_obfuscation=False), stream_mode='cancelled_replay', operations=True)
        self.drained('cancelled')
        self.crash_recovery()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    for name in ('binary', 'output', 'original', 'lease'):
        command.add_argument('--'+name, type=Path, required=True)
    command.add_argument('--port', type=int, default=18164)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_signal, _frame):
            raise TimeoutError('background continuation deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with prior.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
