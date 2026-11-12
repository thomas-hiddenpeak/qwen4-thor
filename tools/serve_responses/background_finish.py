"""Complete preserved crash recovery only: 1 start/1 create/300 seconds."""
import argparse
import json
from pathlib import Path
import shutil
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as prior
from tools.serve_responses import background_resume as resume
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=1, controls=2000, seconds=300)


def requests():
    return dict(chat=prior.requests()['chat'])


def review(original, lease):
    plan, result = read(original/'plan.json'), read(original/'result.json')
    cleanup, crash = read(original/'after-crash-cleanup.json'), read(original/'crash-injection.json')
    require(result['status'] == 'FAILED' and 'Address already in use' in result['error'] and
            result['counts'] == dict(starts=1, chats=2, controls=49), 'wrong interrupted phase')
    require(cleanup['model_exit'] == -9 and cleanup['remaining'] == [] and
            cleanup['ports_released'] and cleanup['lease_continuously_held'], 'crash not cleaned')
    records = prior.base.lifecycle.guard.processes()
    require(all(records.get(int(pid), {}).get('start_ticks') != identity['start_ticks']
                for pid, identity in cleanup['owned'].items()), 'old process still present')
    outcome = read(lease/'exit.json')
    events = [json.loads(line) for line in (lease/'events.jsonl').read_text().splitlines()]
    queries = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    require(outcome['status'] == 'COMMAND_FAILED' and outcome['child_returncode'] == 1 and
            outcome['lease_inherited_by_child'] and
            Path(outcome['command'][-1]).resolve() == original/'plan.json' and
            events[0]['event'] == 'lease_acquired' and events[-1]['event'] == 'lease_released' and
            all(not e.get('conflicts') and not e.get('unreadable_processes') for e in events) and
            queries and all(q['status'] == 'OK' and not q['observed_errors'] for q in queries),
            'original strict lease invalid')
    changed = {'tools/serve_responses/background_acceptance.py',
               'tools/serve_responses/test_background_acceptance.py'}
    for name, digest in plan['sources'].items():
        if name not in changed:
            require(sha(ROOT/name) == digest, 'source drift: '+name)
    require(sha(plan['binary']) == plan['binary_sha256'] == result['binary_sha256'], 'engine drift')
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default drift')
    require(crash['partial']['status'] == 'in_progress' and crash['queued']['status'] == 'queued',
            'original crash checkpoint absent')
    files = {str(p): sha(p) for folder in (original, lease) for p in folder.iterdir() if p.is_file()}
    return dict(original=str(original), lease=str(lease), files=files,
        objects=resume.inventory(original/'objects'), crash=crash,
        binary_sha256=plan['binary_sha256'], original_result='FAILED_RESTART_PORT_PROBE',
        cumulative_limits=dict(starts=3, chats=8, seconds=394.286))


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
    for name in reuse['objects']:
        shutil.copyfile(original/'objects'/name, target/name, follow_symlinks=False)
        (target/name).chmod(0o600)
    require(resume.inventory(target) == reuse['objects'], 'crash snapshot drift')
    plan['mutations'] = ['recover private copy of actual crash state; no new fault or repeated generation']
    save(path, plan)


class Stage(prior.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_BACKGROUND_CRASH_COMPLETION'

    def __init__(self, plan):
        super().__init__(plan)
        reuse = plan['reuse']
        require(review(Path(reuse['original']), Path(reuse['lease'])) == reuse, 'reuse drift')
        require(resume.inventory(self.out/'objects') == reuse['objects'], 'snapshot drift')

    def business(self):
        crash = self.plan['reuse']['crash']
        self.finish_recovery(crash['partial'], crash['queued'])


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
            raise TimeoutError('background crash completion deadline')
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
