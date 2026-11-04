"""Explicit shared-cache seeds after a read-only fixture expected cache hits."""
import argparse
import copy
import io
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_clients import cpu, handoff as h, short as s

BASE = cpu.BASE
PLAN = BASE/'cache-plan.json'
OUT = BASE/'cache-short'
CPU = BASE/'cache-driver-cpu'
LIMITS = dict(starts=1, generation_attempts=6, controls=60, seconds=600)
FILES = ('tools/serve_clients/cache_correction.py',
         'docs/SERVE_CLIENT_CACHE_CORRECTION_2026-11-04.md')
LABELS = ('alpha-seed', 'beta-isolated-seed', 'alpha-read', 'beta-read')


def cache_body(fixture, scope, label):
    s.require(label in LABELS and 'session_id' not in fixture, 'shared fixture only')
    body = copy.deepcopy(fixture)
    body.update(prefix_cache_scope=scope, request_id='shared-'+label,
                cancel_token=secrets.token_hex(32),
                prefix_cache_write_tokens=8192 if label.endswith('seed') else 0)
    return body


def check_cache(values):
    s.require(values == dict(zip(LABELS, (0, 0, 8192, 8192))),
              'shared cache namespace/write/read mismatch')


class Contracts(unittest.TestCase):
    def test_two_clients_explicitly_seed_and_reads_never_write(self):
        fixture = s.pkg.read(h.OUT/'cache-alpha-shared-request.json')['body']
        original = copy.deepcopy(fixture)
        for label, expected in zip(LABELS, (8192, 8192, 0, 0)):
            body = cache_body(fixture, 'a'*64, label)
            s.Draft202012Validator(s.pkg.read(ROOT/'docs/serve-chat-request-v1.schema.json')).validate(body)
            self.assertEqual(body['prefix_cache_write_tokens'], expected)
            self.assertEqual(body['prompt'], fixture['prompt'])
            self.assertNotIn('session_id', body)
        self.assertEqual(fixture, original)

    def test_original_missing_seed_is_preserved_as_failure(self):
        for label in ('cache-alpha-seed', 'cache-beta-isolated', 'cache-alpha-shared', 'cache-beta-shared'):
            body = s.pkg.read(h.OUT/(label+'-request.json'))['body']
            self.assertEqual(body.get('prefix_cache_write_tokens', 0), 0)
        failed = s.pkg.read(h.OUT/'result.json')
        self.assertEqual(failed['status'], 'FAILED')
        self.assertIn('cache namespace/priority mismatch', failed['error'])

    def test_unseeded_read_cross_client_hit_and_missing_read_all_fail(self):
        correct = dict(zip(LABELS, (0, 0, 8192, 8192)))
        check_cache(correct)
        for label, invalid in zip(LABELS, (8192, 8192, 0, 0)):
            with self.assertRaisesRegex(RuntimeError, 'namespace/write/read'):
                check_cache(dict(correct, **{label: invalid}))
        with self.assertRaises(RuntimeError):
            check_cache({})

    def test_private_or_unrecognized_fixture_is_rejected(self):
        for fixture, label in [({'session_id': 'a'*64}, 'alpha-seed'), ({}, 'unknown')]:
            with self.assertRaises(RuntimeError):
                cache_body(fixture, 'a'*64, label)


def test():
    CPU.mkdir()
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    (CPU/'tests.log').write_text(log.getvalue())
    cpu.save(CPU/'result.json', dict(passed=result.wasSuccessful(), tests=result.testsRun,
        sources={name: cpu.sha(ROOT/name) for name in FILES}, gpu_execution=0, http_listeners=0))
    print(log.getvalue(), end='')
    s.require(result.wasSuccessful() and result.testsRun == 4, 'cache driver CPU failed')


def prepare():
    old = s.pkg.read(h.PLAN)
    s.verify_sources(old['sources'])
    s.require(s.pkg.read(h.OUT/'lease/exit.json')['status'] == 'COMMAND_FAILED', 'original guard failure')
    s.require(not PLAN.exists() and not OUT.exists(), 'preserve cache attempt')
    s.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit correction first')
    tested = s.pkg.read(CPU/'result.json')
    s.require(tested['passed'] and tested['tests'] == 4, 'cache fixture gate')
    s.verify_sources(tested['sources'])
    fixture = h.OUT/'cache-alpha-shared-request.json'
    plan = copy.deepcopy(old)
    plan.update(limits=LIMITS, fixture=str(fixture), source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    plan['sources'].update(tested['sources'])
    plan['local'].update({str(p): cpu.sha(p) for p in [h.PLAN, h.OUT/'result.json',
        h.OUT/'lease/exit.json', fixture, CPU/'result.json']})
    plan['reuse'].update(handoff_runtime_unchanged=True, previous_successful_generations=9,
        previous_cancelled_generations=1, previous_real_tools=1,
        reused='terminal handoff, native HTTP, same-ID cancellation/quota, strict JSON/tool/citation; private cache hit/miss and five cache answers',
        remaining='four explicitly seeded shared-cache requests and ordinary recovery',
        original_shared_read_misses_correct=True)
    cpu.save(PLAN, plan)
    return dict(prepared=True, limits=LIMITS, models_started=0)


class Run(s.Run):
    def shared(self):
        fixture = s.pkg.read(self.plan['fixture'])['body']
        scope = secrets.token_hex(32)
        first = cache_body(fixture, scope, LABELS[0])
        preflight = self.request('shared-preflight', dict(request=first), path='/v1/chat/preflight')
        s.require(8192 < preflight['prompt_tokens'] < 9000, 'shared fixture token boundary')
        counts = {}
        for label, client in zip(LABELS, (0, 1, 0, 1)):
            value = self.complete('shared-'+label, cache_body(fixture, scope, label), client)
            s.require(value.message['content'].strip() == '43', 'shared cache answer quality')
            counts[label] = value.usage['prompt_tokens_details']['cached_tokens']
        check_cache(counts)
        cpu.save(OUT/'shared-cache.json', counts)

    def execute(self):
        OUT.mkdir(mode=0o700, exist_ok=True)
        lock = s.a.guard.common_lock()
        info = lock.stat()
        s.require((info.st_dev, info.st_ino) == (66305, 3294067), 'original lock changed')
        lease = s.pkg.read(lock)
        s.require(lease['pid'] == os.getppid() and lease['state'] == 'held' and
                  not s.a.lock_available(lock), 'requires full-phase guard')
        result = dict(status='FAILED', limits=LIMITS, installed=False, long_run=False)
        def interrupted(*_):
            raise RuntimeError('cache short interrupted or expired')
        signals = {v: signal.signal(v, interrupted) for v in
                   (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        signal.alarm(LIMITS['seconds'])
        try:
            s.a.require_idle(OUT/'initial-gpu.json')
            self.launch(True)
            self.shared()
            value = self.complete('scoped-quality', s.a.ordinary(True))
            s.require(value.message['content'].strip() == '710003', 'recovery quality')
            cpu.save(OUT/'final-metrics.json', self.idle('scoped-idle'))
            self.stop(True)
            for folder in ('scoped-model', 'nginx'):
                for name in ('stdout.log', 'stderr.log'):
                    raw = (OUT/folder/name).read_bytes()
                    for secret in [self.policy['gateway_token'],
                                   *(c['api_key'] for c in self.front['clients'])]:
                        s.require(secret.encode() not in raw, 'credential leaked to log')
            self.children.reap(OUT)
            s.a.require_idle(OUT/'final-gpu.json')
            s.prepared_identity(self.plan)
            s.require(self.successes == 5 and self.cancelled == 0, 'cache accounting')
            result.update(status='PASS_BOUNDED_CLIENT_CACHE', successful_generations=5,
                          cancelled_generations=0, cleanup_confirmed=True)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.alarm(0)
            for connection in self.sockets:
                connection.close()
            self.children.cleanup()
            for v, previous in signals.items():
                signal.signal(v, previous)
            result.update(counts=self.counts, seconds=time.monotonic()-self.started)
            cpu.save(OUT/'result.json', result)
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('test', 'prepare', 'check', 'run'))
    args = parser.parse_args()
    if args.action in ('test', 'prepare'):
        value = globals()[args.action]()
    else:
        s.OUT, s.LIMITS = OUT, LIMITS
        plan = s.prepared_identity(s.pkg.read(PLAN))
        if args.action == 'check':
            value = dict(checked=True, models_started=0)
        else:
            s.require(not (OUT/'result.json').exists(), 'preserve cache evidence')
            with s.sup.processes.child_subreaper():
                value = Run(plan).execute()
    print(json.dumps(value))


if __name__ == '__main__':
    main()
