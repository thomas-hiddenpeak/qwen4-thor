"""One startup/request/step after moderation terminal write-result correction.

Closed-peer/deadline behavior is covered by targeted CPU writes. Reuse the
accepted text/vision/Batch business for unchanged computation and ownership.
"""
import argparse
import hashlib
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import moderations_acceptance as accepted
from tools.serve_responses.file_preparation_acceptance import model_steps
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=1, controls=2000, seconds=300)
BASELINE = ROOT/'.q4t-work/serve-moderations-20261111/accepted-baseline'


def requests():
    return dict(moderation=dict(input=accepted.requests()['text']['input'][0]))


def prepare(args):
    identity = read(BASELINE/'identity.json')
    require(sha(BASELINE/'q4t') == identity['binary_sha256'], 'accepted fallback changed')
    previous = read(BASELINE/'runtime-source.json')
    changed = {p for p,h in previous.items() if sha(ROOT/p) != h}
    require(changed == {'src/server/chat_moderations.cpp'}, 'unreviewed compiled source change')
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['rollback'] = identity
    plan['runtime_changed'] = sorted(changed)
    plan['planned'] = dict(starts=1, chats=1, model_steps=1)
    plan['mutations'] = ['isolated own model and state; graceful cleanup; default unchanged']
    plan['derived_requests'] = ['same benign text from accepted fixture, one classifier only',
        'actual current capabilities/configuration and one-RPM/all-token accounting',
        'prior 2-profile/8-request/8-step business remains valid for unchanged behavior',
        'failed socket writes/finite capture deadlines use targeted CPU evidence']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_MODERATIONS_WRITE_BUSINESS'

    def business(self):
        before = self.drained('before')
        self.budget('chats')
        result = self.sdk('moderation', 'moderations_create', requests()['moderation'])
        accepted.check_response(result, [False])
        after = self.drained('after')
        usage = accepted.check_meter(before, after)
        require(after['q4t_requests_total'] == after['q4t_requests_success_total'] == 1
            and after['q4t_requests_error_total'] == 0, 'terminal counters')
        require(model_steps((self.out/'model-1.log').read_text()) == 1, 'model replay')
        sources = read(Path(self.plan['binary']).parent/
                       'generated/q4t/server/chat_protocol_sources.json')
        require(all(sha(ROOT/p) == h for p,h in sources.items()), 'compiled source drift')
        protocol = hashlib.sha256(''.join(p+':'+h+'\n'
            for p,h in sorted(sources.items())).encode()).hexdigest()
        require(read(self.out/'capabilities-1.json')['protocol_source_sha256'] == protocol ==
            read(self.out/'config-1.json')['protocol_source_sha256'], 'native identity drift')
        save(self.out/'acceptance.json', dict(status='ACCEPTED', model_steps=1,
            actual_usage=usage, terminal_success_count=1, protocol_sha256=protocol,
            prior_business_reused=True, failed_writes_cpu=True, default_unchanged=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    for name in ('binary', 'output'): p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19063)
    p = modes.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        def timeout(_signal, _frame): raise TimeoutError('moderation write business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with accepted.foreground.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()
