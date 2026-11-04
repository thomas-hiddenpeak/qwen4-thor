"""Repair only TLS fixture rebind; retain the failed lease and frozen sources."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import deadline_stage as s
from tools.serve_service import package as pkg

ORIGINAL = s.BASE
RETRY = ORIGINAL/'retry-01'
WINDOW = 204
OLD_CHECK = s.check
SELF = 'tools/project_tasks/deadline_retry.py'


class ReusablePeer(s.deadline_probes.Peer):
    def __init__(self, context, case):
        self.context, self.case = context, case
        self.listener = socket.socket()
        try:
            # Both the original listener and the post-close bind must opt in;
            # otherwise a just-closed TCP connection can prevent the rebind.
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(1)
            self.listener.settimeout(2)
            self.port = self.listener.getsockname()[1]
        except BaseException:
            self.listener.close()
            raise
        self.stop = threading.Event()
        self.record = dict(case=case, accepts=0, request_headers=0, sent_bytes=0)
        self.thread = threading.Thread(target=self.serve)


def original():
    first = pkg.read(ORIGINAL/'execution/result.json')
    post = pkg.read(ORIGINAL/'failed-postflight/result.json')
    pkg.require(first['status'] == 'FAILED' and first['cleanup_confirmed'] and
        first['error'] == "OSError(98, 'Address already in use')" and
        first['counts'] == dict(starts=0, generation_attempts=0, controls=0) and
        post['guard_status'] == 'COMMAND_FAILED' and post['identity_records'] ==
        post['completed_probe_receipts'] == post['model_starts'] == post['generation_attempts'] == 0 and
        post['gpu_processes'] == '' and post['lock'] == dict(dev=66305, inode=3294067, released=True),
        'unexpected original failure or resources')
    pkg.require(post['lease_seconds'] + WINDOW <= 210, 'combined original time budget')
    paths = [ORIGINAL/name for name in ('plan.json', 'run.stdout', 'run.stderr')]
    for name in ('execution', 'failed-postflight'):
        paths.extend(p for p in (ORIGINAL/name).rglob('*') if p.is_file())
    return {str(p.relative_to(ORIGINAL)):pkg.sha(p) for p in sorted(paths)}


class FixtureChecks(unittest.TestCase):
    def test_original_listener_sets_reuse_before_bind(self):
        calls = []
        class Fake:
            def setsockopt(self, *args): calls.append(('option', args))
            def bind(self, address): calls.append(('bind', address))
            def listen(self, n): pass
            def settimeout(self, n): pass
            def getsockname(self): return ('127.0.0.1', 12345)
        with patch.object(socket, 'socket', return_value=Fake()):
            peer = ReusablePeer(None, 'headers')
        self.assertEqual(calls, [('option', (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)),
                                 ('bind', ('127.0.0.1', 0))])
        self.assertFalse(peer.thread.is_alive())

    def test_failed_fixture_creation_closes_its_socket(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.bind.side_effect = OSError('bind fixture failure')
        with patch.object(socket, 'socket', return_value=fake), self.assertRaises(OSError):
            ReusablePeer(None, 'headers')
        fake.close.assert_called_once()


def cpu():
    original()
    out = ORIGINAL/'peer-repair-cpu'
    out.mkdir(mode=0o700)
    names = [SELF, 'tools/project_tasks/deadline_stage.py', 'tools/project_tasks/deadline_probes.py']
    sources = {name:pkg.sha(ROOT/name) for name in names}
    for name in names:
        path = out/'source'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FixtureChecks)
    identities = [t.id() for t in suite]
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    pkg.save(out/'result.json', dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        names=identities, sources=sources, http_requests=0, model_starts=0))
    pkg.require(result.wasSuccessful() and not result.skipped, 'peer repair CPU')
    return dict(passed=True, tests_run=result.testsRun)


def prepare():
    pkg.require(not RETRY.exists(), 'preserve retry')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit peer repair first')
    prior = original()
    plan = OLD_CHECK()
    fixed = pkg.read(ORIGINAL/'peer-repair-cpu/result.json')
    pkg.require(fixed['passed'] and fixed['tests_run'] == 2, 'peer CPU gate')
    for name, digest in fixed['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'peer CPU source drift')
    RETRY.mkdir(mode=0o700)
    # Copy the exact existing receipts/sources only, excluding CA private test
    # material and mutable task fixtures. Original paths stay untouched.
    for name in (*plan['receipts'], 'peer-repair-cpu'):
        for path in (ORIGINAL/name).rglob('*'):
            relative = path.relative_to(ORIGINAL/name)
            if path.is_file() and (str(relative) in ('result.json', 'tests.log') or relative.parts[0] == 'source'):
                target = RETRY/name/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    shutil.copyfile(ORIGINAL/'baseline.json', RETRY/'baseline.json')
    plan.update(gpu_window_seconds=WINDOW, source_files=s.sources(), original_attempt=prior,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        first_probe_attempts=1, total_probe_attempts_limit=13)
    plan['receipts']['peer-repair-cpu'] = pkg.sha(ORIGINAL/'peer-repair-cpu/result.json')
    pkg.save(RETRY/'plan.json', plan)
    configure()
    check()
    return dict(prepared=True, http_requests=0, model_starts=0)


def check():
    plan = OLD_CHECK()
    for name, digest in plan['original_attempt'].items():
        pkg.require(pkg.sha(ORIGINAL/name) == digest, 'first failed attempt changed')
    pkg.require(plan['first_probe_attempts'] == 1 and plan['total_probe_attempts_limit'] == 13, 'fixed repair probe budget')
    return plan


def configure():
    s.BASE = s.ATTEMPT = RETRY
    s.PLAN, s.OUT = RETRY/'plan.json', RETRY/'execution'
    s.GPU_WINDOW = WINDOW
    s.check = check
    s.deadline_probes.Peer = ReusablePeer
    for name in ('BASE', 'ATTEMPT', 'PLAN', 'OUT', 'HISTORICAL', 'TASKS', 'PORT', 'TLS_PORT', 'LIMITS', 'GPU_WINDOW'):
        setattr(s.prior, name, getattr(s, name))


def guard_command():
    return [sys.executable, '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output', str(s.OUT/'lease'), '--', sys.executable, '-B', str(ROOT/SELF), 'run']


def guarded():
    check()
    pkg.require(not s.OUT.exists(), 'preserve corrected execution')
    with (RETRY/'run.stdout').open('xb') as stdout, (RETRY/'run.stderr').open('xb') as stderr:
        return subprocess.run(guard_command(), cwd=ROOT, stdout=stdout, stderr=stderr).returncode


def report(action):
    from tools.project_tasks import deadline_report as r
    prior_collect, prior_audit = r.collect, r.audit
    r.READERS = (*r.READERS, SELF)
    def collect():
        data = prior_collect()
        for name in pkg.read(s.PLAN)['original_attempt']:
            data['original/'+name] = (ORIGINAL/name).read_bytes()
        for path in (RETRY/'peer-repair-cpu').rglob('*'):
            if path.is_file(): data[str(path.relative_to(RETRY))] = path.read_bytes()
        return data
    def audit(data):
        value = prior_audit(data)
        plan = json.loads(data['plan.json'])
        for name, digest in plan['original_attempt'].items():
            pkg.require(r.sha(data['original/'+name]) == digest, 'original archive identity')
        post = json.loads(data['original/failed-postflight/result.json'])
        first = json.loads(data['original/execution/result.json'])
        pkg.require(post['guard_status'] == 'COMMAND_FAILED' and post['model_starts'] ==
            post['generation_attempts'] == post['identity_records'] == post['completed_probe_receipts'] == 0 and
            first['error'] == "OSError(98, 'Address already in use')", 'original pre-model failure')
        fixed = json.loads(data['peer-repair-cpu/result.json'])
        pkg.require(fixed['passed'] and fixed['tests_run'] == 2 and not fixed['failures'] and
                    not fixed['errors'] and not fixed['skipped'], 'peer CPU result')
        for name, digest in fixed['sources'].items():
            pkg.require(r.sha(data['peer-repair-cpu/source/'+name]) == digest == plan['source_files'][name], 'peer source identity')
        value.update(retry_gpu_seconds=value['gpu_seconds'], original_gpu_seconds=post['lease_seconds'],
            gpu_seconds=value['gpu_seconds'] + post['lease_seconds'], original_probe_attempts=1,
            total_probe_attempts=13, peer_repair_tests=2, original_probe_cleanup_failure_preserved=True)
        pkg.require(value['gpu_seconds'] <= 210, 'combined fixed lease time')
        return value
    r.collect, r.audit = collect, audit
    if action == 'postflight': r.postflight(); return dict(postflight=True)
    if action == 'seal': r.seal()
    return r.verify()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'guard', 'run', 'postflight', 'seal', 'verify'))
    action = parser.parse_args().action
    if action == 'cpu': print(json.dumps(cpu()))
    elif action == 'prepare': print(json.dumps(prepare()))
    else:
        configure()
        if action == 'guard': raise SystemExit(guarded())
        elif action == 'run': s.Run(check()).execute()
        else: print(json.dumps(report(action)))
