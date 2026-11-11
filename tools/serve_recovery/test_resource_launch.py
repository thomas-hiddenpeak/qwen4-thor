"""Actual privileged entry and new-instance cleanup; CPU roles only."""
from copy import deepcopy
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, deployment, journal, ledger
from tools.serve_recovery import outer_instance, outer_policy, outer_store, outer_tree
from tools.serve_recovery import resource_install as install, resource_kernel as kernel
from tools.serve_recovery import resource_launch as launch, resource_reconcile, store
from tools.serve_recovery import test_outer_reconcile as fixture
from tools.serve_recovery import test_resource_install as installation_tests


def payload(path, keep_instance):
    desc = deployment.read_descriptor(path); now = store.stamp()
    owner = dict(pid=os.getpid(), start_ticks=journal.process_start(os.getpid()), boot_id=now.boot_id)
    with store.Store(path, desc['service_id']) as core:
        fixture.cycle(core, owner, leave=True)
        if not keep_instance:
            fixture.transact(core, 'fault', generation=core.state['generations']['model'],
                             fingerprint='f'*64, unknown=False)
            fixture.transact(core, 'quarantine', reason='device_failure')
            fixture.transact(core, 'desired', state='STOPPED')
    if keep_instance:
        (path/'payload-ready').touch(); time.sleep(10); return
    pid = os.fork()
    if pid == 0:
        # Guardian descendants do not retain the instance FD. This CPU orphan
        # deliberately holds the original writer to exercise kill-before-lock.
        for entry in list(Path('/proc/self/fd').iterdir()):
            if int(entry.name) > 2:
                try: os.close(int(entry.name))
                except OSError: pass
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with store.Store(path, desc['service_id']):
            (path/'orphan-pid').write_text(str(os.getpid()))
            (path/'payload-ready').touch(); time.sleep(10)
        os._exit(0)
    fixture.until(lambda: (path/'payload-ready').exists())
    os._exit(23)


def prepare(path, mode):
    public = install.read_public(Path(os.environ[launch.DIRECTORY])); sid = public['service_id']
    cap = launch.inherited(sid)
    desc = dict(version=1, service_id=sid, package=str(path/'sealed'), manifest_sha256='a'*64)
    deployment.write_descriptor(path, desc)
    with store.Store(path, sid, initialize=True) as core:
        core.require_service_contract(); fixture.transact(core, 'desired', state='RUNNING')
    binding = access_store.binding(path, sid)
    outer_store.initialize_bound(binding, outer_policy.initial(sid, store.stamp()))
    outer_store.commit_once(binding, 'configure', dict(enabled=True))
    instance = outer_instance.Instance(path, sid).__enter__(); tree = outer_tree.Tree().__enter__()
    attempt = outer_store.commit_once(binding, 'request', dict(core=fixture.read_core(path, sid),
        ticket=str(uuid.uuid4()), manifest_sha256='a'*64, cleanup_verified=True,
        identity_valid=True, custody=tree.binding(instance), resource=cap.binding()))['state']['pending']
    command = [sys.executable, '-I', '-B', __file__, '--payload', str(path), mode]
    wrapped = cap.dispatch(command, sys.executable, ROOT/'tools/serve_recovery/resource_launch.py')
    owner = tree.launch(wrapped, env=dict(os.environ), pass_fds=(cap.fd, instance.fd),
                        instance=instance, attempt=attempt)
    if mode != 'lost-spawn':
        outer_store.commit_once(binding, 'spawned', dict(ticket=attempt['ticket'], owner=owner))
    fixture.until(lambda: (path/'payload-ready').exists())
    if mode != 'busy': fixture.until(lambda: tree.poll() is not None)
    print(json.dumps(dict(prepared=True, resource=attempt['resource'], owner=owner)), flush=True)
    # Deliberately lose the original outer while its own bounded CPU subtree
    # remains. The root test runner is its subreaper and retains final cleanup.
    os._exit(0)


def reconcile(path, mode):
    desc = deployment.read_descriptor(path); sid = desc['service_id']; cap = launch.inherited(sid)
    binding = access_store.binding(path, sid)
    original = fixture.read_outer(binding)
    if mode in ('wrong-domain', 'wrong-manifest', 'busy'):
        try:
            with outer_instance.Instance(path, sid) as instance: resource_reconcile.retire(instance, cap)
        except (ValueError, BlockingIOError):
            assert fixture.read_outer(binding) == original
            print(json.dumps(dict(rejected=True, no_retirement=True)), flush=True); return
        raise AssertionError('unsafe resource retirement was accepted')
    # The orphan holds the writer; read the durable private record without
    # trying to take that lock before the actual resource cleanup.
    before = json.loads((path/store.STATE).read_text())['state']
    with outer_instance.Instance(path, sid) as instance:
        if mode == 'core-interrupt':
            replace = os.replace
            def fail(source, destination, **kwargs):
                if source == store.PENDING: raise OSError('injected resource retirement rename')
                return replace(source, destination, **kwargs)
            with mock.patch.object(os, 'replace', side_effect=fail):
                try: resource_reconcile.retire(instance, cap); raise AssertionError('missing interruption')
                except OSError: pass
            assert (path/store.PENDING).exists()
            assert fixture.read_outer(binding)['pending'] == original['pending']
        if mode == 'outer-receipt':
            transact = outer_store.Store.transact
            def lost(writer, *args, **kwargs):
                transact(writer, *args, **kwargs); raise OSError('injected lost resource receipt')
            with mock.patch.object(outer_store.Store, 'transact', new=lost):
                try: resource_reconcile.retire(instance, cap); raise AssertionError('missing receipt failure')
                except OSError: pass
        result = resource_reconcile.retire(instance, cap)
        after = fixture.read_core(path, sid); current = fixture.read_outer(binding)
        assert not any(after['active'].values()) and current['pending'] is None
        assert all(after[k] == before[k] for k in ('desired', 'quarantine', 'generations',
            'attempts', 'failures', 'not_before', 'circuit_until'))
        assert after['desired'] == 'STOPPED' and after['quarantine'] == 'device_failure'
        assert current['total'] == original['total'] == 1 and current['starts'] == original['starts']
        assert resource_reconcile.retire(instance, cap)['already_retired']
        assert cap.scope.empty(); cap.scope.member('outer')
        print(json.dumps(dict(reconciled=True, protection_preserved=True,
                              budgets_preserved=True, result=result)), flush=True)


def scenario(path, mode):
    if mode.startswith('prepare:'): return prepare(path, mode.split(':', 1)[1])
    if mode.startswith('retire:'): return reconcile(path, mode.split(':', 1)[1])
    public = install.read_public(Path(os.environ[launch.DIRECTORY])); sid = public['service_id']
    if mode == 'wrong-fd':
        source = int(os.environ[launch.ENTRY_FD]); replacement = os.open('/dev/null', os.O_WRONLY)
        os.dup2(replacement, source); os.close(replacement)
        try: launch.inherited(sid); raise AssertionError('ordinary FD accepted')
        except ValueError: print(json.dumps(dict(rejected=True))); return
    cap = launch.inherited(sid)
    assert launch.DIRECTORY not in os.environ and launch.ENTRY_FD not in os.environ
    cap.scope.member('outer'); original = cap.binding()
    command = [sys.executable, '-I', '-B', __file__, '--exec-check', str(path), str(cap.fd)]
    wrapped = cap.dispatch(command, sys.executable, ROOT/'tools/serve_recovery/resource_launch.py')
    if mode == 'wrong-publication': wrapped[wrapped.index('--expected')+1] = '0'*64
    result = subprocess.run(wrapped, pass_fds=(cap.fd,), capture_output=True, text=True, timeout=3)
    if mode == 'wrong-publication':
        assert result.returncode != 0 and not (path/'executed').exists()
        assert cap.scope.empty(); print(json.dumps(dict(rejected=True))); return
    assert result.returncode == 0, result.stderr
    cap.scope.member('outer'); assert cap.scope.empty()
    assert cap.binding() == original
    print(json.dumps(dict(outer_uid=os.geteuid(), child=json.loads(result.stdout),
                         source_bound=True, no_privilege_escalation=True)), flush=True)


def runtime_helper(path, operation):
    from tools.serve_recovery import control, test_outer_runtime
    if operation == 'prepare':
        os.environ['Q4T_TEST_OUTPUT_DIR'] = str(path)
        fixture = test_outer_runtime.RuntimeTests(); fixture.setUp()
        config = dict(state=str(fixture.state), package=str(fixture.package), digest=fixture.digest,
            base=fixture.fixture.base, mode='simple-gateway', activate=True)
        (path/'runtime-config.json').write_text(json.dumps(config))
        print(json.dumps(dict(service_id=fixture.sid, state=str(fixture.state))), flush=True)
        # The parent owns this fixture's final deletion, after actual cleanup.
        os._exit(0)
    config = json.loads((path/'runtime-config.json').read_text())
    sid = deployment.read_descriptor(Path(config['state']))['service_id']
    if operation == 'stop':
        print(json.dumps(control.request(sid, 'stop', timeout=4)), flush=True); return
    deadline = time.monotonic()+6
    while time.monotonic() < deadline:
        try:
            value = control.request(sid, 'status', timeout=.2)
            if value['business_ready']:
                print(json.dumps(value), flush=True); return
        except (OSError, ValueError): pass
        time.sleep(.02)
    raise TimeoutError('resource-bound CPU Guardian did not become ready')


class LaunchTests(installation_tests.InstallationTests):
    def setUp(self):
        super().setUp()
        self.selected = kernel.policy(128, 256, 64)
        base = ROOT/'.q4t-work/serve-outer-recovery-20261112/resource-launch-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        self.path = base/str(uuid.uuid4()); self.path.mkdir(mode=0o700)
        os.chown(self.path, self.uid, self.gid)
        self.tree = outer_tree.Tree().__enter__()
        self.addCleanup(self.finish_tree)

    def finish_tree(self):
        try:
            if self.tree.root is not None: self.tree.drain(term_seconds=.05, kill_seconds=2)
        finally:
            self.tree.close()
            import shutil
            shutil.rmtree(self.path)

    def run_entry(self, mode, directory=None, expect_success=True):
        directory = directory or self.directory
        command = [sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/resource_launch.py'),
            '--directory', str(directory), '--', sys.executable, '-I', '-B', __file__,
            '--scenario', str(self.path), mode]
        read_fd, write_fd = os.pipe()
        try:
            if self.tree.root is None:
                self.tree.launch(command, env=dict(os.environ), stdio=write_fd)
                process = self.tree.root
            else:
                process = subprocess.Popen(command, env=dict(os.environ), stdin=subprocess.DEVNULL,
                    stdout=write_fd, stderr=write_fd)
                self.processes.append(process)
        finally: os.close(write_fd)
        with os.fdopen(read_fd, 'rb') as stream:
            ready, _, _ = select.select([stream], [], [], 6)
            self.assertTrue(ready, 'CPU resource launch deadline')
            raw = stream.read(16385)
        code = process.wait(timeout=6)
        if not expect_success:
            self.assertNotEqual(code, 0); return raw.decode()
        self.assertEqual(code, 0, raw.decode()); return json.loads(raw)

    def test_entry_drops_privileges_and_child_closes_capability_before_exec(self):
        self.initialize(); result = self.run_entry('normal')
        self.assertEqual(result['outer_uid'], self.uid)
        self.assertEqual(result['child']['uid'], self.uid)
        self.assertTrue(result['child']['fd_closed']); self.assertTrue(result['child']['service_member'])

    def test_forged_fd_or_publication_never_executes_payload(self):
        self.initialize()
        for mode in ('wrong-fd', 'wrong-publication'):
            self.assertTrue(self.run_entry(mode)['rejected'])
        self.assertFalse((self.path/'executed').exists())

    def test_removed_installation_cannot_enter_or_execute(self):
        self.initialize(); install.remove(self.directory)
        self.assertIn('revoked', self.run_entry('normal', expect_success=False))
        self.assertFalse((self.path/'executed').exists())

    def check_retirement(self, mode):
        self.initialize(); self.run_entry('prepare:'+mode)
        with kernel.Scope(install.load(self.directory)['record']) as scope: self.assertFalse(scope.empty())
        result = self.run_entry('retire:'+mode)
        self.assertTrue(result['protection_preserved']); self.assertTrue(result['budgets_preserved'])
        return result

    def test_new_instance_cleans_original_orphan_before_writer_lock_and_preserves_protection(self):
        self.check_retirement('normal')

    def test_unrecorded_root_birth_is_reconciled_only_by_original_resource_domain(self):
        self.check_retirement('lost-spawn')

    def test_fresh_empty_domain_cannot_retire_the_original_populated_domain(self):
        self.initialize(); self.run_entry('prepare:normal')
        other = self.parent/'other'
        install.initialize(other, self.sid, self.uid, self.gid, self.selected)
        self.assertTrue(self.run_entry('retire:wrong-domain', other)['rejected'])
        with kernel.Scope(install.load(self.directory)['record']) as scope: self.assertFalse(scope.empty())

    def test_changed_deployment_cannot_kill_original_payload(self):
        self.initialize(); self.run_entry('prepare:normal')
        path = self.path/'service.json'; value = json.loads(path.read_text())
        value['manifest_sha256'] = 'b'*64; path.write_text(json.dumps(value))
        self.assertTrue(self.run_entry('retire:wrong-manifest')['rejected'])
        with kernel.Scope(install.load(self.directory)['record']) as scope: self.assertFalse(scope.empty())

    def test_live_original_instance_prevents_new_domain_cleanup(self):
        self.initialize(); self.run_entry('prepare:busy')
        self.assertTrue(self.run_entry('retire:busy')['rejected'])
        with kernel.Scope(install.load(self.directory)['record']) as scope: self.assertFalse(scope.empty())

    def test_interrupted_core_retirement_resumes_without_reopening_budget(self):
        self.check_retirement('core-interrupt')

    def test_lost_outer_retirement_receipt_replays_without_another_kill_or_charge(self):
        self.assertTrue(self.check_retirement('outer-receipt')['result']['already_retired'])

    def test_actual_native_guardian_and_cpu_roles_enter_bound_service_domain(self):
        def helper(operation):
            result = subprocess.run([sys.executable, '-I', '-B', __file__, '--runtime-helper',
                str(self.uid), str(self.gid), str(self.path), operation], capture_output=True,
                text=True, timeout=8, env=dict(os.environ,
                    Q4T_GUARDIAN_BINARY=str(ROOT/'.q4t-work/guardian-build/q4t-guardian')))
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        setup = helper('prepare'); self.sid = setup['service_id']
        self.selected = kernel.policy(512, 768, 128); public = self.initialize()
        command = [sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/resource_launch.py'),
            '--directory', str(self.directory), '--', sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/test_outer_runtime.py'), '--worker',
            str(self.path/'runtime-config.json')]
        self.tree.launch(command, env=dict(os.environ))
        ready = helper('status'); self.assertTrue(ready['outer_supervised'])
        expected = '0::/'+str(Path(public['record']['root']).relative_to(kernel.CGROUP))+'/service'
        for role in ('guardian', 'controller', 'gateway', 'model'):
            pid = ready[role+'_pid']
            self.assertEqual(Path(f'/proc/{pid}/cgroup').read_text().strip(), expected)
            for fd in Path(f'/proc/{pid}/fd').iterdir():
                try: target = os.readlink(fd)
                except FileNotFoundError: continue
                self.assertFalse(target.endswith('/cgroup.procs'), 'privileged migration FD leaked')
        state = Path(setup['state'])
        policy = json.loads((state/'outer-recovery/policy.json').read_text())['state']['policy']
        self.assertEqual(policy['pending']['resource']['publication'], public)
        self.assertEqual(policy['total'], 1)
        self.assertTrue(helper('stop')['cleanup_confirmed'])
        self.assertEqual(self.tree.root.wait(timeout=6), 0)
        final = json.loads((state/store.STATE).read_text())['state']
        self.assertEqual(final['desired'], 'STOPPED'); self.assertFalse(any(final['active'].values()))
        self.assertTrue(install.remove(self.directory)['removed'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--scenario': scenario(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == '--payload': payload(Path(sys.argv[2]), sys.argv[3] == 'busy')
    elif len(sys.argv) > 1 and sys.argv[1] == '--exec-check':
        fd = int(sys.argv[3]); closed = False
        try: os.fstat(fd)
        except OSError: closed = True
        member = Path('/proc/self/cgroup').read_text().strip().endswith('/service')
        assert closed and member and os.geteuid() != 0
        assert 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
        (Path(sys.argv[2])/'executed').touch()
        print(json.dumps(dict(uid=os.geteuid(), fd_closed=closed, service_member=member)), flush=True)
    elif len(sys.argv) > 1 and sys.argv[1] == '--runtime-helper':
        uid, gid = map(int, sys.argv[2:4]); os.setgroups([])
        os.setresgid(gid, gid, gid); os.setresuid(uid, uid, uid)
        runtime_helper(Path(sys.argv[4]), sys.argv[5])
    else: unittest.main(module=None)
