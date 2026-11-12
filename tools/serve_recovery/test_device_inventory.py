"""Independent initial-device evidence must not turn unknown into idle."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_recovery import device_inventory as inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sys = self.root/'sys'
        self.proc = self.root/'proc'
        (self.proc/'sys/kernel/random').mkdir(parents=True)
        (self.proc/'sys/kernel/random/boot_id').write_text(
            'a8d8af72-16b7-4f19-b47d-4fa009cfa45c\n')
        (self.proc/'sys/kernel/osrelease').write_text('6.8.12-1021-tegra\n')
        (self.proc/'devices').write_text('Character devices:\n'
            '  1 mem\n195 nvidia\n195 nvidiactl\n195 nvidia-modeset\n'
            '226 drm\n486 nvidia-uvm\n489 nvidia-caps\n\n'
            'Block devices:\n  8 sd\n')
        for name in sorted(inventory.MODULES):
            path = self.sys/'module'/name
            (path/'holders').mkdir(parents=True)
            (path/'version').write_text('595.78\n')
            (path/'srcversion').write_text('A'*24+'\n')
            (path/'initstate').write_text('live\n')
            (path/'refcnt').write_text('0\n')
        self.holders('nvidia', ['nvidia_uvm', 'nvidia_modeset'])
        self.holders('nvidia_modeset', ['nvidia_drm'])

    def holders(self, name, holders):
        path = self.sys/'module'/name
        for link in (path/'holders').iterdir():
            link.unlink()
        for holder in holders:
            (path/'holders'/holder).symlink_to(self.sys/'module'/holder)
        (path/'refcnt').write_text(str(len(holders))+'\n')

    def inspect(self):
        value = inventory.inspect(sys_root=self.sys, proc_root=self.proc)
        self.assertFalse(value['launch_authorized'])
        self.assertFalse(value['model_actions'])
        return value

    def test_quiet_full_graph_is_only_observation_never_authorization(self):
        value = self.inspect()
        self.assertEqual(value['status'], 'QUIET_OBSERVATION')
        self.assertEqual(value['outstanding'], [])
        self.assertEqual(value['reason'],
                         'requires_complete_kernel_fence_and_initial_qualification')
        self.assertEqual(len(value['facts_sha256']), 64)
        majors = value['facts']['character_devices']
        self.assertEqual([item['major'] for item in majors], [195, 226, 486, 489])
        self.assertEqual(majors[0]['names'],
                         ['nvidia', 'nvidia-modeset', 'nvidiactl'])

    def test_kernel_source_id_preserves_actual_width_instead_of_padding(self):
        path = self.sys/'module/nvidia/srcversion'
        original = '01406A18AAA4C43F4A8F257'
        fingerprints = []
        for source in (original, original+'0'):
            path.write_text(source+'\n')
            value = self.inspect()
            self.assertEqual(value['status'], 'QUIET_OBSERVATION')
            self.assertEqual(value['facts']['modules'][0]['source'], source)
            fingerprints.append(value['facts_sha256'])
        self.assertNotEqual(*fingerprints)
        path.write_text('A'*65+'\n')
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')

    def test_extra_kernel_references_block_even_without_any_process_listing(self):
        (self.sys/'module/nvidia_uvm/refcnt').write_text('3\n')
        (self.sys/'module/nvidia/refcnt').write_text('6\n')
        value = self.inspect()
        self.assertEqual(value['status'], 'BUSY')
        self.assertEqual(value['outstanding'], [dict(module='nvidia', references=4),
            dict(module='nvidia_uvm', references=3)])
        self.assertFalse((self.proc/'1').exists())

    def test_unaccounted_or_cyclic_holders_cannot_create_reference_exemptions(self):
        self.holders('nvidia', ['unknown_driver'])
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        self.holders('nvidia', ['nvidia_uvm', 'nvidia_modeset'])
        self.holders('nvidia_uvm', ['nvidia'])
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        self.holders('nvidia_uvm', [])
        (self.sys/'module/nvidia/refcnt').write_text('1\n')
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')

    def test_missing_module_and_foreign_holder_path_are_unknown(self):
        path = self.sys/'module/nvidia_uvm'
        path.rename(path.with_name('removed_uvm'))
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        path.with_name('removed_uvm').rename(path)
        link = self.sys/'module/nvidia/holders/nvidia_uvm'
        link.unlink()
        link.symlink_to(self.root)
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')

    def test_unloading_bad_counts_or_mismatched_builds_never_appear_quiet(self):
        path = self.sys/'module/nvidia_uvm'
        for name, bad in [('initstate', 'going\n'), ('refcnt', '-1\n'),
                          ('refcnt', '0junk\n'), ('refcnt', '2147483648\n'),
                          ('srcversion', 'unidentified\n'),
                          ('version', '595.79\n')]:
            with self.subTest(name=name, value=bad):
                old = (path/name).read_bytes()
                (path/name).write_text(bad)
                self.assertEqual(self.inspect()['status'], 'UNKNOWN')
                (path/name).write_bytes(old)

    def test_unknown_alias_and_missing_device_registration_are_rejected(self):
        path = self.proc/'devices'
        original = path.read_text()
        for text in [original.replace('489 nvidia-caps', '489 nvidia-new-compute'),
                     original.replace('486 nvidia-uvm\n', ''),
                     original.replace('195 nvidiactl', '196 nvidiactl'),
                     original.replace('226 drm', '226 drm\n226 drm'),
                     original.replace('Block devices:', 'Other devices:')]:
            with self.subTest(registry=text):
                path.write_text(text)
                self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        path.write_text(original)

    def test_read_failures_and_oversized_or_invalid_identity_are_unknown(self):
        path = self.proc/'sys/kernel/random/boot_id'
        original = path.read_bytes()
        for value in [b'bad\n', b'x'*4097, b'\xff']:
            path.write_bytes(value)
            self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        path.write_bytes(original)
        with mock.patch.object(inventory, 'read', side_effect=OSError('unreadable')):
            self.assertEqual(self.inspect()['status'], 'UNKNOWN')
        version = self.sys/'module/nvidia/version'
        version.unlink()
        version.symlink_to(self.proc/'sys/kernel/osrelease')
        self.assertEqual(self.inspect()['status'], 'UNKNOWN')

    def test_any_change_between_two_complete_snapshots_invalidates_observation(self):
        first = inventory.snapshot(sys_root=self.sys, proc_root=self.proc)
        for kind in ('references', 'inode', 'character_devices', 'boot_id'):
            second = deepcopy(first)
            if kind in ('references', 'inode'):
                second['modules'][0][kind] += 1
            elif kind == 'character_devices':
                second[kind][0]['major'] += 1
            else:
                second[kind] = '25f50ab7-7af8-4de1-9402-dd27f7babdcd'
            with self.subTest(kind=kind), mock.patch.object(
                    inventory, 'snapshot', side_effect=[first, second]):
                value = self.inspect()
                self.assertEqual(value['status'], 'UNKNOWN')
                self.assertIsNone(value['facts'])


if __name__ == '__main__':
    unittest.main()
