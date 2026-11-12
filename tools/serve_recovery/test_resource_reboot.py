"""Historical resource-bound launch records and actual renewed CPU domains."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, deployment, ledger, lifecycle
from tools.serve_recovery import outer_instance, outer_policy, outer_reconcile, outer_store, store
from tools.serve_recovery import resource_install as install, resource_launch as launch
from tools.serve_recovery import test_outer_reconcile as fixture
from tools.serve_recovery import test_resource_renew as renewal_tests


def history(path, mode):
    original = json.loads((path/'original-resource.json').read_text())
    sid = original['publication']['service_id']; now = store.stamp()
    old = ledger.Stamp(original['publication']['record']['boot_id'], now.monotonic_ns)
    desc = dict(version=1, service_id=sid, package=str(path/'sealed'), manifest_sha256='d'*64)
    deployment.write_descriptor(path, desc)
    with store.Store(path, sid, initialize=True, clock=lambda: old) as core:
        core.require_service_contract(); fixture.transact(core, 'desired', state='RUNNING')
    binding = access_store.binding(path, sid)
    outer_store.initialize_bound(binding, outer_policy.initial(sid, old))
    outer_store.commit_once(binding, 'configure', dict(enabled=True), clock=lambda: old)
    owner = dict(pid=123, start_ticks=456, boot_id=old.boot_id)
    attempt = outer_store.commit_once(binding, 'request', dict(core=fixture.read_core(path, sid),
        ticket=str(uuid.uuid4()), manifest_sha256=desc['manifest_sha256'], cleanup_verified=True,
        identity_valid=True, custody=dict(supervisor=owner, instance_token=str(uuid.uuid4()),
            tree_scope=str(uuid.uuid4())), resource=original), clock=lambda: old)['state']['pending']
    stamp = now if mode == 'current-owner' else old
    with store.Store(path, sid, clock=lambda: stamp) as core:
        fixture.cycle(core, dict(owner, boot_id=stamp.boot_id), leave=True)
        fixture.transact(core, 'fault', generation=core.state['generations']['model'],
                         fingerprint='f'*64, unknown=False)
        fixture.transact(core, 'quarantine', reason='device_failure')
    # Actual supported offline stop advances the clock, leaving the gateway
    # grant without a process birth receipt. Original resource binding remains.
    result = lifecycle.request_stop(path, desc['package'], desc['manifest_sha256'])
    assert result['stop_durable'] and not result['cleanup_confirmed']
    if mode == 'advance':
        intermediate = install.read_public(Path(original['directory']))
        stamp = ledger.Stamp(intermediate['record']['boot_id'], now.monotonic_ns)
        outer_store.commit_once(binding, 'prepare_reboot', dict(ticket=attempt['ticket'],
            core=fixture.read_core(path, sid), resource=dict(directory=original['directory'],
                publication=intermediate)), clock=lambda: stamp)
    print(json.dumps(dict(prepared=True)), flush=True)


def reconcile(path, mode):
    desc = deployment.read_descriptor(path); sid = desc['service_id']
    cap = launch.inherited(sid); binding = access_store.binding(path, sid)
    before = fixture.read_core(path, sid); old_outer = fixture.read_outer(binding)
    if mode in ('missing-capability', 'wrong-domain', 'current-owner'):
        with outer_instance.Instance(path, sid) as instance:
            try:
                outer_reconcile.reboot(instance, resource=None if mode == 'missing-capability' else cap)
                raise AssertionError('unproven reboot cleanup accepted')
            except ValueError: pass
        assert fixture.read_core(path, sid) == before and fixture.read_outer(binding) == old_outer
        print(json.dumps(dict(rejected=True)), flush=True); return
    if mode == 'interrupt':
        original = os.replace
        def fail(src, dst, **kwargs):
            if src == store.PENDING: raise OSError('injected resource reboot retirement')
            return original(src, dst, **kwargs)
        with outer_instance.Instance(path, sid) as instance:
            with mock.patch.object(os, 'replace', side_effect=fail):
                try: outer_reconcile.reboot(instance, resource=cap); raise AssertionError('missing fault')
                except OSError: pass
        assert fixture.read_outer(binding)['pending']['cleanup']['resource'] == cap.binding()
        assert (path/store.PENDING).exists()
    with outer_instance.Instance(path, sid) as instance:
        result = outer_reconcile.reboot(instance, resource=cap)
    after = fixture.read_core(path, sid); current = fixture.read_outer(binding)
    assert not any(after['active'].values()) and current['pending'] is None
    assert all(after[k] == before[k] for k in ('desired', 'quarantine', 'generations',
        'attempts', 'failures', 'not_before', 'circuit_until'))
    assert current['total'] == old_outer['total'] == 1 and current['starts'] == old_outer['starts']
    assert after['desired'] == 'STOPPED' and after['quarantine'] == 'device_failure'
    assert cap.scope.empty(); cap.scope.member('outer')
    print(json.dumps(dict(reconciled=True, original_state_preserved=True, result=result)), flush=True)


class RebootResourceTests(renewal_tests.RenewalTests):
    def setup_history(self, mode):
        old = self.old_epoch()
        (self.path/'original-resource.json').write_text(json.dumps(dict(
            directory=str(self.directory), publication=install.public_value(old))))
        (self.path/'original-resource.json').chmod(0o644)
        if mode == 'advance':
            middle = deepcopy(old); middle['revision'] += 2
            middle['previous_record'] = deepcopy(old['record'])
            middle['record']['boot_id'] = str(uuid.uuid4())
            install.commit(self.directory, middle)
        result = subprocess.run([sys.executable, '-I', '-B', __file__, '--history',
            str(self.uid), str(self.gid), str(self.path), mode], capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_reboot(self, mode, directory=None):
        command = [sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/resource_launch.py'),
            '--directory', str(directory or self.directory), '--', sys.executable, '-I', '-B',
            __file__, '--reconcile', str(self.path), mode]
        result = subprocess.run(command, capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_bound_old_domain_resolves_unrecorded_birth_after_offline_stop(self):
        self.setup_history('normal'); self.assertTrue(self.run_reboot('normal')['original_state_preserved'])

    def test_resource_bound_attempt_cannot_fall_back_to_metadata_only_reboot(self):
        self.setup_history('normal'); self.assertTrue(self.run_reboot('missing-capability')['rejected'])

    def test_different_installation_cannot_retire_old_resource_bound_tickets(self):
        self.setup_history('normal'); other = self.parent/'other'
        install.initialize(other, self.sid, self.uid, self.gid, self.selected)
        self.assertTrue(self.run_reboot('wrong-domain', other)['rejected'])

    def test_known_current_boot_owner_is_not_retired_by_old_resource_lineage(self):
        self.setup_history('current-owner'); self.assertTrue(self.run_reboot('current-owner')['rejected'])

    def test_resource_reboot_checkpoint_resumes_with_new_instance_after_write_failure(self):
        self.setup_history('normal'); self.assertTrue(self.run_reboot('interrupt')['original_state_preserved'])

    def test_reboot_during_cleanup_advances_boot_without_reselecting_old_slots(self):
        self.setup_history('advance'); self.assertTrue(self.run_reboot('advance')['original_state_preserved'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--history':
        uid, gid = map(int, sys.argv[2:4]); os.setgroups([])
        os.setresgid(gid, gid, gid); os.setresuid(uid, uid, uid)
        history(Path(sys.argv[4]), sys.argv[5])
    elif len(sys.argv) > 1 and sys.argv[1] == '--reconcile': reconcile(Path(sys.argv[2]), sys.argv[3])
    else: unittest.main(module=None)
