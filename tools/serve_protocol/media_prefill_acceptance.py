"""One start/three requests: original video through full/cold/warm prefill.

Retain the exact standard clip and question. Additional logprobs expose numeric
changes. This targeted regression does not qualify a release or claim speed.
"""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import re
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import profile_acceptance as q
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=3, controls=1000, seconds=300)
SUCCESS = 'PASS_ORIGINAL_VIDEO_PREFILL_REGRESSION'


def requests(media=True):
    require(media, 'regression needs actual media')
    original = q.profile_evidence.media_request(
        (ROOT / q.profile_evidence.MEDIA_FILE).read_bytes())
    original.update(include=['message.output_text.logprobs'], top_logprobs=3)
    full = copy.deepcopy(original)
    full['prompt_cache_options'] = dict(mode='explicit', ttl='30m')
    return dict(full=full, implicit_cold=original,
                implicit_warm=copy.deepcopy(original))


# Reuse the strict managed configuration, readiness, custody and cleanup paths.
q.requests = requests
q.limits_for = lambda _profile: LIMITS


def output(value):
    require(value['status'] == 'completed' and not value.get('error'),
            'video completion missing')
    messages = [x for x in value['output'] if x['type'] == 'message']
    require(messages and all(x['role'] == 'assistant' for x in messages),
            'video assistant missing')
    parts = [p for x in messages for p in x['content']]
    require(parts and all(p['type'] == 'output_text' for p in parts),
            'original video refused or produced no text')
    require(json.loads(''.join(p['text'] for p in parts)) ==
            dict(start='red', end='blue'), 'actual frame colors differ')
    require(all(p.get('logprobs') for p in parts), 'actual logprobs missing')
    return parts


class Stage(q.Stage):
    success_status = SUCCESS

    def business(self):
        results = {label: self.request(label, body, '/v1/responses')
                   for label, body in self.plan['requests'].items()}
        save(self.out / 'comparison.json', dict(results=results))
        projected = {name: output(value) for name, value in results.items()}
        require(projected['full'] == projected['implicit_cold'] ==
                projected['implicit_warm'], 'full/cold/warm output or logprobs differ')
        for i, (name, value) in enumerate(results.items()):
            usage = value['usage']
            detail = usage['input_tokens_details']
            require(usage['input_tokens'] == 1069 and
                    detail['cached_tokens'] == (1062 if i == 2 else 0) and
                    detail['cache_write_tokens'] == (1062 if i == 1 else 0),
                    'actual cache boundary differs: ' + name)
        log = (self.out / 'primary-model.log').read_text()
        require(len(re.findall(r'event=write prefix=1062 ttl=1800 saved=1 bytes=[1-9]',
                               log)) == 1, 'actual SSD write missing')
        require(len(re.findall(r'event=restore prefix=1062 ms=\S+ error=none '
                               r'source=shared layout=target', log)) == 1,
                'actual target restore missing')
        save(self.out / 'business-checks.json', dict(full_cold_warm_exact=True,
             actual_frame_colors=dict(start='red', end='blue'), prompt_tokens=1069,
             prefix_tokens=1062, actual_ssd_write_and_restore=True))


def finalize(out):
    plan, result = read(out / 'plan.json'), read(out / 'result.json')
    require(result['status'] == SUCCESS and result['counts']['starts'] == 1 and
            result['counts']['chats'] == 3 and result['seconds'] < LIMITS['seconds'],
            'targeted business result or budget differs')
    for name, digest in {**plan['sources'], **plan['protected'],
                         **plan['native_assets']}.items():
        require(sha(ROOT / name) == digest, 'postflight identity drift: ' + name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'engine drift')
    q.bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out / 'lease'
    guard_exit = read(lease / 'exit.json')
    queries = [json.loads(line) for line in
               (lease / 'nvml-queries.jsonl').read_text().splitlines()]
    cleanup = read(out / 'cleanup.json')
    require(guard_exit['status'] == 'COMPLETED' and
            guard_exit['child_returncode'] == 0 and
            guard_exit['compute_backend'] == 'nvml' and
            guard_exit['lease_inherited_by_child'], 'strict environment failed')
    require(queries and all(x.get('status') == 'OK' and x.get('returncode') == 0
                            for x in queries), 'NVML observation failed')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'cleanup failed')
    guard = q.encrypted.foreground.base.lifecycle.guard
    records = guard.processes()
    require(not any(records.get(int(pid), {}).get('start_ticks') == row['start_ticks']
                    for pid, row in cleanup['owned'].items()), 'owned process remains')
    q.background.probe_ports((plan['port'], plan['operations_port']))
    with guard.common_lock().open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    save(out / 'acceptance.json', dict(status='ACCEPT_ORIGINAL_VIDEO_PREFILL_REPAIR',
         counts=result['counts'], seconds=result['seconds'], nvml_queries=len(queries),
         binary_sha256=plan['binary_sha256'], cleanup_complete=True,
         full_cold_warm_exact=True, default_unchanged=True, release_qualified=False,
         performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    for name in ('binary', 'package', 'candidate-build', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19069)
    p = sub.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    p = sub.add_parser('finalize')
    p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        args.candidate_release = None
        args.capacity_config = None
        q.prepare(args)
        path = args.output / 'plan.json'
        plan = read(path)
        plan['sources'][str(Path(__file__).resolve())] = sha(Path(__file__))
        plan['scope'] = __doc__
        plan['planned'] = dict(starts=1, requests=3,
            full_cold_warm_logprobs_exact=True, performance_claim=False)
        save(path, plan)
    elif args.mode == 'finalize':
        finalize(args.output.resolve())
    else:
        def deadline(*_):
            raise TimeoutError('bounded original video regression deadline')
        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds'] - 40)
        with q.encrypted.foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
