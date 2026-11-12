"""Owned CPU host installations with real privilege drop and kernel domains.

Synthetic engine verification is injected into the selected *test* package
only, after UID drop. Production host entry/outer STOPPED paths execute from
the actual protected source. GPU lease acquisition is forbidden by the fixture.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, host_artifact as artifact, host_install as host
from tools.serve_recovery import outer_manager, resource_install as resources, resource_kernel as kernel, service, store
from tools.serve_recovery import test_native_bundle, test_resource_install as fixtures


def prepare(path, *, native=False, cold=False):
    os.environ['Q4T_TEST_OUTPUT_DIR'] = str(path)
    fixture = test_native_bundle.BundleTests(); fixture.setUp()
    fixture.base['python'] = dict(path=str(Path(sys.executable).resolve()),
                                 sha256=bundle.engine.sha(Path(sys.executable).resolve()))
    if native:
        shutil.copyfile(ROOT/'.q4t-work/guardian-build/q4t-guardian', fixture.guardian)
        build = json.loads(fixture.build.read_text())
        build['guardian_sha256'] = bundle.engine.sha(fixture.guardian)
        fixture.save(fixture.build, build)
    receipt = fixture.prepare(); package = Path(receipt['package'])
    injection = '''\n# Explicit CPU fixture: verification substitution only after UID drop.\nif os.geteuid() != 0:\n    import json\n    from tools.serve_recovery import bundle as cpu_bundle, service as cpu_service\n    cpu_bundle.engine.verify = lambda *_a, **_kw: json.loads(%r)\n    def cpu_no_gpu(*_a, **_kw): raise AssertionError('GPU forbidden in host CPU fixture')\n    cpu_service.acquire_lease = cpu_no_gpu\n\n''' % json.dumps(fixture.base)
    if cold:
        private_lease = str(fixture.directory/'private-cold-cpu-lease')
        injection = injection.replace('    cpu_service.acquire_lease = cpu_no_gpu\n',
            '''    cpu_original_lease = cpu_service.acquire_lease
    def cpu_private_lease(path):
        if str(path) != %r: raise AssertionError('only the CPU private lease is allowed')
        return cpu_original_lease(path)
    cpu_service.acquire_lease = cpu_private_lease
    if 'Q4T_MANAGED_LEASE_FD' in os.environ:
        from tools.serve_recovery import child as cpu_child
        cpu_fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        cpu_child.verify_lease(cpu_fd, %r)
        cpu_info = os.fstat(cpu_fd)
        Path(%r).write_text(json.dumps(dict(uid=os.geteuid(), device=cpu_info.st_dev, inode=cpu_info.st_ino)))
''' % (private_lease, private_lease, str(fixture.directory/'cpu-managed-lease.json')))
    manifest = json.loads((package/'manifest.json').read_text())
    for name in ('host_entry', 'outer_daemon', *(('cold_verify',) if cold else ())):
        relative = 'source/tools/serve_recovery/'+name+'.py'; script = package/relative
        script.chmod(0o600)
        script.write_text(script.read_text().replace("if __name__ == '__main__':", injection+"if __name__ == '__main__':"))
        script.chmod(0o400); manifest['files'][relative] = bundle.engine.sha(script)
    if cold: manifest['lock_path'] = str(fixture.directory/'private-cold-cpu-lease')
    bundle.engine.save(package/'manifest.json', manifest); digest = bundle.engine.sha(package/'manifest.json')
    launcher = package/'bin/q4t'; launcher.chmod(0o600)
    launcher.write_bytes(bundle.launcher(digest, fixture.base['python']['path'])); launcher.chmod(0o500)
    state = fixture.directory/'state'; service.initialize(package, digest, state)
    outer_manager.initialize(state, package, digest)
    outer_manager.configure(state, package, digest, enabled=True)
    desc = service.descriptor(state, package, digest)
    result = dict(package=str(package), digest=digest, state=str(state), service_id=desc['service_id'])
    (path/'cpu-engine.json').write_text(json.dumps(fixture.base))
    (path/'fixture.json').write_text(json.dumps(result)); print(json.dumps(result), flush=True)
    os._exit(0)  # Parent owns final fixture deletion; no background process.


def change(path, mode):
    value = json.loads((path/'fixture.json').read_text()); state = Path(value['state'])
    if mode == 'running':
        with store.Store(state, value['service_id']) as core:
            core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                              operation='desired', args=dict(state='RUNNING')))
    elif mode == 'outer-disabled':
        from tools.serve_recovery import access_store, outer_store
        outer_store.commit_once(access_store.binding(state, value['service_id']),
                                 'configure', dict(enabled=False))
    elif mode == 'live-prepare':
        config = dict(state=str(state), package=value['package'], digest=value['digest'],
            base=json.loads((path/'cpu-engine.json').read_text()), mode='simple-gateway', activate=True)
        (path/'runtime-config.json').write_text(json.dumps(config))
    elif mode == 'live-status':
        from tools.serve_recovery import control
        deadline = time.monotonic()+6
        while time.monotonic() < deadline:
            try:
                status = control.request(value['service_id'], 'status', timeout=.2)
                if status['business_ready']:
                    print(json.dumps(status), flush=True); return
            except (OSError, ValueError): pass
            time.sleep(.02)
        raise TimeoutError('owned native CPU service did not become ready')


class HostTests(fixtures.InstallationTests):
    def setUp(self):
        super().setUp()
        self.host = self.parent/'host'
        base = ROOT/'.q4t-work/serve-host-install-20261112/fixtures'
        base.mkdir(parents=True, exist_ok=True)
        self.path = base/str(uuid.uuid4()); self.path.mkdir(mode=0o700)
        os.chown(self.path, self.uid, self.gid)
        self.addCleanup(lambda: shutil.rmtree(self.path))
        result = self.helper('prepare'); self.fixture = result
        self.sid = result['service_id']; super().initialize()

    def helper(self, operation):
        result = subprocess.run([sys.executable, '-I', '-B', __file__, '--helper', str(self.path), operation],
            user=self.uid, group=self.gid, extra_groups=[], env=host.environment(),
            capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def initialize_host(self):
        run = subprocess.run
        def observed(command, **options):
            result = run(command, **options)
            if result.returncode and '--check' in command:
                sys.stderr.buffer.write(result.stderr[:16384])
            return result
        with mock.patch.object(host.subprocess, 'run', side_effect=observed):
            return host.initialize(self.host, self.fixture['package'], self.fixture['digest'],
                self.fixture['state'], self.sid, self.uid, self.gid, self.directory)

    def state_files(self):
        state = Path(self.fixture['state'])
        return {str(p.relative_to(state)): p.read_bytes() for p in state.rglob('*.json')}

    def enter(self, *, daemon=False, operation='enter', success=True):
        command = [sys.executable, '-I', '-B', __file__, '--entry', str(self.host), operation]
        if daemon: command.append('--daemon')
        process = subprocess.Popen(command, env=host.environment(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        self.processes.append(process)
        out, error = process.communicate(timeout=8)
        if not success:
            self.assertNotEqual(process.returncode, 0); return error.decode()
        self.assertEqual(process.returncode, 0, error.decode()); result = json.loads(out)
        if daemon and result.get('state') == 'STOPPED':
            with kernel.Scope(resources.load(self.directory)['record']) as scope:
                deadline = time.monotonic()+3
                while not scope.empty('outer') or not scope.empty():
                    if time.monotonic() >= deadline: self.fail('owned STOPPED daemon did not leave domain')
                    time.sleep(.01)
        return result

    def test_cli_install_enable_disable_preserves_core_and_uses_protected_source(self):
        before = self.state_files()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = service.main(['host-entry', 'init', '--directory', str(self.host),
                '--package', self.fixture['package'], '--digest', self.fixture['digest'],
                '--state', self.fixture['state'], '--service-id', self.sid,
                '--runtime-uid', str(self.uid), '--runtime-gid', str(self.gid),
                '--resource-directory', str(self.directory)])
        self.assertEqual(result, 0); first = json.loads(output.getvalue())
        self.assertFalse(first['automatic_start']); self.assertFalse(first['installation']['enabled'])
        self.assertTrue(first['runtime']['checked']); self.assertEqual(first['runtime']['runtime_uid'], self.uid)
        original = host.load(self.host); self.initialize_host()
        self.assertEqual(original, host.load(self.host))
        for enabled in (True, True, False):
            self.assertFalse(host.configure(self.host, enabled=enabled)['core_intent_changed'])
        self.assertTrue(host.status(self.host)['publication_current'])
        self.assertEqual(before, self.state_files())
        copied = self.host/'source/tools/serve_recovery/host_entry.py'
        self.assertEqual(copied.stat().st_uid, 0); self.assertEqual(copied.stat().st_mode & 0o777, 0o444)
        result = subprocess.run([sys.executable, '-I', '-B', '-c',
            'import os,sys; os.open(sys.argv[1], os.O_WRONLY)', str(copied)],
            user=self.uid, group=self.gid, extra_groups=[], capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'PermissionError', result.stderr)

    def test_installed_root_entry_permanently_drops_uid_and_keeps_stopped_zero_gpu(self):
        self.initialize_host(); host.configure(self.host, enabled=True); before = self.state_files()
        result = self.enter(); self.assertEqual(result['state'], 'STOPPED')
        self.assertTrue(result['cleanup_confirmed']); self.assertEqual(before, self.state_files())
        with kernel.Scope(resources.load(self.directory)['record']) as scope:
            self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))

    def test_private_umask_keeps_source_traversable_and_admin_private(self):
        previous = os.umask(0o077)
        try: result = self.initialize_host()
        finally: os.umask(previous)
        self.assertTrue(result['runtime']['checked'])
        self.assertEqual((self.host/'admin').stat().st_mode & 0o777, 0o700)
        for path in (self.host/'source', self.host/'source/tools',
                     self.host/'source/tools/serve_recovery'):
            self.assertEqual(path.stat().st_uid, 0); self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_installed_daemon_resume_keeps_original_stopped_and_zero_starts(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        result = self.enter(daemon=True); self.assertEqual(result['state'], 'STOPPED')
        policy = json.loads((Path(self.fixture['state'])/'outer-recovery/policy.json').read_text())['state']['policy']
        self.assertEqual(policy['total'], 0); self.assertIsNone(policy['pending'])

    def test_explicit_stop_remains_available_when_host_entry_disabled(self):
        self.initialize_host(); self.helper('running')
        self.assertIn('disabled', self.enter(success=False))
        result = self.enter(operation='stop'); self.assertTrue(result['cleanup_confirmed'])
        state = json.loads((Path(self.fixture['state'])/store.STATE).read_text())['state']
        self.assertEqual(state['desired'], 'STOPPED')

    def test_partial_source_copy_resumes_same_installation_without_core_mutation(self):
        before = self.state_files(); original = artifact.write_new; once = False
        def interrupt(path, raw):
            nonlocal once
            if not once:
                once = True
                with mock.patch.object(store.Store, 'write_all', side_effect=OSError('injected copy loss')):
                    original(path, raw)
            return original(path, raw)
        with mock.patch.object(artifact, 'write_new', side_effect=interrupt), self.assertRaises(OSError):
            self.initialize_host()
        pending = host.load(self.host); self.assertEqual(pending['phase'], 'prepared')
        ready = host.finish(self.host)['installation']
        self.assertEqual(ready['installation_id'], pending['installation_id'])
        self.assertEqual(ready['revision'], 1); self.assertFalse(ready['enabled'])
        self.assertEqual(before, self.state_files())

    def test_partial_first_intent_does_not_publish_source_or_reset_core(self):
        before = self.state_files(); original = store.Store.write_all; once = False
        def partial(fd, raw):
            nonlocal once
            if not once and host.FORMAT.encode() in raw:
                once = True; os.write(fd, raw[:23]); raise OSError('injected first intent loss')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=partial), self.assertRaises(OSError):
            self.initialize_host()
        self.assertTrue(once); self.assertFalse((self.host/'source').exists())
        self.assertFalse((self.host/host.PUBLIC).exists())
        self.assertFalse(self.initialize_host()['installation']['enabled'])
        self.assertEqual(before, self.state_files())

    def test_lost_ready_publication_repairs_copy_without_resetting_history(self):
        original = host.publish
        def fail(directory, value):
            if value['phase'] == 'ready': raise OSError('injected ready publication')
            return original(directory, value)
        with mock.patch.object(host, 'publish', side_effect=fail), self.assertRaises(OSError):
            self.initialize_host()
        before = host.load(self.host); self.assertEqual(before['phase'], 'ready')
        host.finish(self.host)
        self.assertEqual(host.load(self.host), before); self.assertEqual(host.public(self.host), before)

    def test_lost_published_private_authority_cannot_bootstrap(self):
        self.initialize_host(); (self.host/'admin'/host.PRIVATE).unlink()
        with self.assertRaisesRegex(ValueError, 'authority is missing'): self.initialize_host()
        self.assertFalse((self.host/'admin'/host.PRIVATE).exists())

    def test_mutable_package_tamper_is_rejected_after_drop_without_changing_snapshot(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        relative = 'tools/serve_recovery/ledger.py'; copied = self.host/'source'/relative
        before = copied.read_bytes(); original = Path(self.fixture['package'])/'source'/relative
        original.chmod(0o600); original.write_bytes(original.read_bytes()+b'\n# CPU tamper\n')
        self.assertIn('recovery file identity changed', self.enter(success=False))
        self.assertEqual(copied.read_bytes(), before)
        self.assertEqual(json.loads((Path(self.fixture['state'])/store.STATE).read_text())['state']['desired'], 'STOPPED')

    def test_active_remove_revokes_future_entry_without_signalling_own_payload(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        process, _ = self.run_worker('join')
        with self.assertRaisesRegex(ValueError, 'explicit stop'): host.remove(self.host)
        self.assertIsNone(process.poll()); self.assertEqual(host.load(self.host)['phase'], 'revoking')
        self.assertIn('disabled', self.enter(success=False))
        process.terminate(); process.wait(timeout=2)
        removed = host.remove(self.host); self.assertEqual(removed, host.remove(self.host))
        self.assertTrue(removed['history_retained']); self.assertTrue(removed['resource_installation_retained'])
        self.assertEqual(resources.load(self.directory)['phase'], 'ready')

    def test_untrusted_interpreter_hash_is_rejected_before_creating_installation(self):
        package = Path(self.fixture['package']); path = package/'manifest.json'
        value = json.loads(path.read_text()); value['python']['sha256'] = '0'*64
        bundle.engine.save(path, value); self.fixture['digest'] = bundle.engine.sha(path)
        with self.assertRaisesRegex(ValueError, 'digest differs'): self.initialize_host()
        self.assertFalse(self.host.exists())

    def test_selected_source_symlink_is_not_followed_and_initial_copy_can_resume(self):
        source = Path(self.fixture['package'])/'source/tools/serve_recovery/ledger.py'
        raw = source.read_bytes(); source.unlink(); source.symlink_to(self.path/'fixture.json')
        with self.assertRaises(OSError): self.initialize_host()
        before = host.load(self.host); self.assertEqual(before['phase'], 'prepared')
        source.unlink(); source.write_bytes(raw)
        os.chown(source, self.uid, self.gid); source.chmod(0o400)
        host.finish(self.host)
        self.assertEqual(host.load(self.host)['installation_id'], before['installation_id'])

    def test_outer_disable_is_not_implicitly_reversed_by_host_enable(self):
        self.initialize_host(); self.helper('outer-disabled'); before = self.state_files()
        with self.assertRaisesRegex(ValueError, 'explicit outer recovery enable'):
            host.configure(self.host, enabled=True)
        self.assertFalse(host.load(self.host)['enabled']); self.assertEqual(before, self.state_files())

    def test_drifted_protected_snapshot_blocks_entry_and_is_not_silently_repaired(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        source = self.host/'source/tools/serve_recovery/ledger.py'
        source.write_bytes(source.read_bytes()+b'\n# root CPU tamper\n')
        self.assertIn('digest differs', self.enter(success=False))
        with self.assertRaisesRegex(ValueError, 'digest differs'): host.finish(self.host)
        # Disable remains available despite the auxiliary source corruption.
        self.assertFalse(host.configure(self.host, enabled=False)['installation']['enabled'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--helper':
        if sys.argv[3] in ('prepare', 'prepare-native'):
            prepare(Path(sys.argv[2]), native=sys.argv[3] == 'prepare-native')
        else: change(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == '--entry':
        host.entry(Path(sys.argv[2]), operation=sys.argv[3], daemon='--daemon' in sys.argv)
    else: unittest.main(module=None)
