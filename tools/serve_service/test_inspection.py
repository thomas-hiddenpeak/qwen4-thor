"""Failure boundaries and real flock/procfs checks without model or HTTP."""
import copy
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import inspection as probe, package as pkg
from tools.evalscope import gpu_exclusive as guard

EMPTY = dict(present=[], candidates=[], unreadable=[], complete=True)


class Case(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'build/service-inspection-tests'
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run_dir = self.root/'run'
        self.run_dir.mkdir()
        self.package = self.root/'package'
        self.package.mkdir()
        (self.package/'q4t').write_bytes(b'not an executable')
        self.lock = self.root/'lease.lock'
        self.lock.write_text('{"pid":42,"state":"held","output":"old"}')
        self.control = dict(version=2, owner=dict(pid=2**30, start_ticks=1,
            boot_id=probe.boot_id()), observation_context=probe.current_context(),
            package=str(self.package), manifest_sha256='a'*64,
            lock_path=str(self.lock))
        pkg.save(self.run_dir/'controller.json', self.control)
        for obj, name, kwargs in (
            (pkg, 'verify', dict(return_value=dict(repository=str(ROOT),
                                                  settings=dict(port=18123)))),
            (guard, 'common_lock', dict(return_value=self.lock)),
            (probe, 'process_snapshot', dict(return_value=copy.deepcopy(EMPTY))),
            (probe, 'listeners', dict(return_value=[])),
            (probe.nvml_process_query, 'query', dict(return_value=dict(
                status='OK', pids=[], device_count=1))),
        ):
            patch = mock.patch.object(obj, name, **kwargs)
            setattr(self, name+'_mock', patch.start())
            self.addCleanup(patch.stop)

    def observe(self):
        return probe.inspect(self.run_dir)

    def test_stale_held_text_is_read_only_and_actually_available(self):
        pkg.save(self.run_dir/'terminal.json', dict(state='FAILED',
                                               cleanup_confirmed=False))
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        lock_before = (self.lock.stat().st_ino, self.lock.read_bytes())
        result = self.observe()
        self.assertTrue(result['cleanup_observed'])
        self.assertEqual(result['run_state'], 'INACTIVE_OBSERVED')
        self.assertEqual(result['lock']['state'], 'AVAILABLE')
        self.assertEqual(result['lock']['historical_record']['state'], 'held')
        self.assertFalse(result['history']['terminal']['cleanup_confirmed'])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.run_dir.iterdir()})
        self.assertEqual(lock_before, (self.lock.stat().st_ino, self.lock.read_bytes()))

    def test_busy_real_lock_is_never_unlocked_by_inspection(self):
        with self.lock.open('rb') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.observe()
            self.assertTrue(result['complete'])
            self.assertFalse(result['cleanup_observed'])
            self.assertEqual(result['lock']['state'], 'BUSY')
            with self.lock.open('rb') as outsider:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_inspector_holds_only_its_probe_until_gpu_read_finishes(self):
        def gpu(**_):
            with self.lock.open('rb') as outsider:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return dict(status='OK', pids=[], device_count=1)
        self.query_mock.side_effect = gpu
        self.assertTrue(self.observe()['cleanup_observed'])
        with self.lock.open('rb') as outsider:
            fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_legacy_and_mismatched_scope_never_claim_complete(self):
        for context in (None, dict(uid=-1)):
            value = dict(self.control)
            if context is None:
                value.pop('observation_context')
                value['version'] = 1
            else:
                value['observation_context'] = context
            pkg.save(self.run_dir/'controller.json', value)
            result = self.observe()
            self.assertFalse(result['complete'])
            self.assertFalse(result['cleanup_observed'])
            self.assertEqual(result['resource_availability'], 'UNKNOWN')
            self.assertEqual(result['lock']['state'], 'AVAILABLE')

    def test_gpu_failure_is_unknown_not_idle(self):
        self.query_mock.return_value = dict(status='ERROR', error='unavailable')
        result = self.observe()
        self.assertFalse(result['cleanup_observed'])
        self.assertIn('gpu_observation_unavailable', result['errors'])

    def test_foreign_gpu_is_busy_without_blame_on_old_run(self):
        self.query_mock.return_value = dict(status='OK', pids=[12345], device_count=1)
        result = self.observe()
        self.assertEqual(result['run_state'], 'OWNER_ABSENT_RESOURCES_BUSY')
        self.assertEqual(result['processes']['present'], [])

    def test_unreadable_process_or_port_blocks_absence_claim(self):
        self.process_snapshot_mock.return_value = dict(
            **{**EMPTY, 'complete':False, 'unreadable':[42]})
        result = self.observe()
        self.assertFalse(result['complete'])
        self.process_snapshot_mock.return_value = EMPTY
        self.listeners_mock.side_effect = PermissionError()
        result = self.observe()
        self.assertIn('listener_observation_unavailable', result['errors'])
        self.assertFalse(result['cleanup_observed'])

    def test_metadata_transition_and_inode_replacement_fail_closed(self):
        def gpu(**_):
            pkg.save(self.run_dir/'controller.json', dict(self.control, created_unix=1))
            self.lock.unlink()
            self.lock.write_text('{}')
            return dict(status='OK', pids=[], device_count=1)
        self.query_mock.side_effect = gpu
        result = self.observe()
        self.assertIn('run_records_changed_during_observation', result['errors'])
        self.assertIn('lock_inode_changed', result['errors'])
        self.assertFalse(result['cleanup_observed'])

    def test_resource_transition_and_recovery_marker_are_not_free(self):
        self.listeners_mock.side_effect = [[], [dict(inode=1, family='tcp')]]
        self.assertFalse(self.observe()['complete'])
        self.listeners_mock.side_effect = None
        self.lock.with_name(self.lock.name+'.recovery-required').touch()
        result = self.observe()
        self.assertTrue(result['complete'])
        self.assertFalse(result['cleanup_observed'])
        self.assertEqual(result['resource_availability'], 'BUSY')

    def test_bad_record_symlink_and_fifo_are_bounded_rejections(self):
        path = self.run_dir/'controller.json'
        for kind in ('duplicate', 'symlink', 'fifo'):
            path.unlink()
            if kind == 'duplicate':
                path.write_text('{"owner":1,"owner":2}')
            elif kind == 'symlink':
                path.symlink_to(self.lock)
            else:
                os.mkfifo(path)
            result = self.observe()
            self.assertFalse(result['cleanup_observed'])
            self.assertTrue(result['errors'])

    def test_live_owner_or_zombie_remains_visible(self):
        for state in ('S', 'Z'):
            self.process_snapshot_mock.return_value = dict(EMPTY, present=[
                dict(pid=42, start_ticks=1, ppid=1, state=state,
                     roles=['controller'])])
            result = self.observe()
            self.assertEqual(result['run_state'], 'OWNER_PRESENT')
            self.assertFalse(result['cleanup_observed'])


class ProcCase(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'build/service-inspection-tests'
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.proc = self.root/'proc'
        self.proc.mkdir()
        self.package = self.root/'package'
        self.package.mkdir()
        (self.package/'q4t').write_text('image')
        self.boot = probe.boot_id()
        self.control = dict(owner=dict(pid=42, start_ticks=100, boot_id=self.boot))
        patch = mock.patch.object(probe, 'PROC', self.proc)
        patch.start()
        self.addCleanup(patch.stop)

    def row(self, pid, start, *, ppid=1, image=None, args=(), state='S'):
        path = self.proc/str(pid)
        path.mkdir()
        fields = [state, str(ppid), *(['0']*17), str(start), '0']
        (path/'stat').write_text(f'{pid} (process ) name) '+ ' '.join(fields))
        (path/'cmdline').write_bytes(b'\0'.join(s.encode() for s in args))
        if image:
            (path/'exe').symlink_to(image)

    def snapshot(self, known=()):
        return probe.process_snapshot(known, self.control, self.package, self.boot)

    def test_pid_reuse_is_not_owner_and_old_pgid_is_not_used(self):
        self.row(42, 200, image='/usr/bin/python3')
        result = self.snapshot([dict(role='controller', **self.control['owner'])])
        self.assertEqual(result['present'], [])
        self.assertEqual(result['candidates'], [])
        self.assertTrue(result['complete'])

    def test_orphan_package_image_and_unrecorded_helper_are_candidates(self):
        self.row(43, 101, image=self.package/'q4t')
        self.row(44, 102, image='/usr/bin/python3', args=[
            str(self.package/'source/tools/serve_service/backend.py'), '--run', 'x'])
        result = self.snapshot()
        self.assertEqual([r['pid'] for r in result['candidates']], [43, 44])

    def test_new_unreadable_process_is_unknown_preexisting_one_is_ignored(self):
        self.row(43, 99)  # Nondumpable processes which predate this generation.
        self.assertTrue(self.snapshot()['complete'])
        self.row(44, 101)
        # Existing process with an unreadable executable is not an exited one.
        original = probe.inode
        def read(path):
            if path == self.proc/'44/exe':
                raise PermissionError()
            return original(path)
        with mock.patch.object(probe, 'inode', side_effect=read):
            result = self.snapshot()
        self.assertFalse(result['complete'])
        self.assertEqual(result['unreadable'], [44])

    def test_ipv4_and_ipv6_any_address_listeners(self):
        (self.proc/'net').mkdir()
        for name, address in (('tcp', '00000000'), ('tcp6', '0'*32)):
            (self.proc/'net'/name).write_text('sl local_address remote_address\n'
                f'0: {address}:46CB {address}:0000 0A 0 0 0 0 0 987 0\n')
        rows = probe.listeners(18123)
        self.assertEqual(len(rows), 2)
        (self.proc/'net/tcp6').write_text('sl local_address\nbroken\n')
        with self.assertRaises(ValueError):
            probe.listeners(18123)

    def test_scheduling_state_changes_do_not_hide_identity_changes(self):
        a = dict(processes=dict(EMPTY, present=[dict(
            pid=42, start_ticks=100, state='S', roles=['controller'])]))
        b = copy.deepcopy(a)
        b['processes']['present'][0]['state'] = 'R'
        self.assertEqual(probe.stable_resources(a), probe.stable_resources(b))
        b['processes']['present'][0]['start_ticks'] += 1
        self.assertNotEqual(probe.stable_resources(a), probe.stable_resources(b))


if __name__ == '__main__':
    unittest.main()
