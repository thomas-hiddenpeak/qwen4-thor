"""Owned root installation tests; manager calls simulated, unit parser real.

No host unit is registered in this suite. Actual manager integration is a
separate explicitly scoped test using only newly named /run units.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import host_install as host, host_systemd as adapter, store
from tools.serve_recovery import test_host_install as fixtures


class Manager:
    def __init__(self, roots):
        self.roots = roots; self.calls = []; self.active = {}; self.overrides = {}; self.fail = False

    def __call__(self, *arguments):
        self.calls.append(arguments)
        if self.fail: raise OSError('injected optional manager failure')
        args = [x for x in arguments if x not in ('--runtime', '--no-block')]
        operation = args[0]
        if operation == 'daemon-reload': return ''
        name = args[1]
        paths = [root/name for root in self.roots.values() if (root/name).exists()]
        if operation == 'show':
            value = dict(Id=name, LoadState='loaded' if paths else 'not-found',
                ActiveState='active' if self.active.get(name) else 'inactive',
                SubState='running' if self.active.get(name) else 'dead',
                FragmentPath=str(paths[0]) if paths else '', DropInPaths='')
            value.update(self.overrides.get(name, {}))
            return '\n'.join(k+'='+v for k, v in value.items())
        if operation in ('start', 'stop'):
            self.active[name] = operation == 'start'; return ''
        if operation in ('enable', 'disable'): return ''
        raise AssertionError('unexpected manager command '+repr(arguments))


class AdapterTests(fixtures.HostTests):
    def setUp(self):
        super().setUp(); self.initialize_host(); host.configure(self.host, enabled=True)
        self.unit_roots = {scope: self.parent/('units-'+scope) for scope in ('runtime', 'persistent')}
        for path in self.unit_roots.values(): path.mkdir(mode=0o755); path.chmod(0o755)
        self.manager = Manager(self.unit_roots)
        self.patches = (mock.patch.object(adapter, 'DIRECTORIES', self.unit_roots),
                        mock.patch.object(adapter, 'manager', side_effect=self.manager))
        for patch in self.patches: patch.start(); self.addCleanup(patch.stop)

    def install_trigger(self): return adapter.install(self.host, 'runtime')
    def record(self): return adapter.load(self.host, host.load(self.host))

    def trigger_entry(self):
        value = host.load(self.host)
        script = self.host/'source/tools/serve_recovery/host_entry.py'
        return subprocess.run([value['artifact']['python']['path'], '-I', '-B', str(script),
            '--ensure', '--systemd-trigger', '--directory', str(self.host)],
            env=host.environment(), capture_output=True, text=True, timeout=5)

    def test_render_is_optional_and_real_systemd_parser_accepts_owned_units(self):
        before = self.state_files(); value = host.public(self.host)
        units = adapter.render(self.host, value)
        self.assertFalse(self.manager.calls)
        self.assertFalse((self.host/'admin'/adapter.RECORD).exists())
        for name, raw in units.items():
            (self.unit_roots['runtime']/name).write_bytes(raw)
            self.assertNotIn(b'nginx', raw)
        service, timer = adapter.names(value)
        self.assertIn(b'--systemd-trigger', units[service]); self.assertNotIn(b'ExecStop', units[service])
        self.assertIn(b'OnUnitInactiveSec=30s', units[timer])
        result = subprocess.run(['/usr/bin/systemd-analyze', 'verify', '--man=no',
            *(str(self.unit_roots['runtime']/name) for name in units)], env=host.environment(),
            capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adapter.quote('/run/a$B%q"\\z'), '"/run/a$$B%%q\\"\\\\z"')
        with self.assertRaises(ValueError): adapter.quote('/run/a\nExecStart=bad')
        self.assertEqual(before, self.state_files())

    def test_explicit_registration_and_enablement_leave_model_intent_unchanged(self):
        before = self.state_files(); result = self.install_trigger(); original = self.record()
        self.assertTrue(result['registered']); self.assertFalse(result['enabled'])
        self.assertFalse(any('start' in c for c in self.manager.calls))
        self.install_trigger(); self.assertEqual(self.record(), original)
        self.assertTrue(host.status(self.host)['host_startup_registered'])
        self.assertTrue(adapter.configure(self.host, enabled=True)['enabled'])
        self.assertIn(('--no-block', 'start', adapter.names(host.load(self.host))[1]), self.manager.calls)
        self.assertFalse(adapter.configure(self.host, enabled=False)['enabled'])
        self.assertEqual(before, self.state_files())

    def test_native_entry_without_optional_registration_never_calls_manager(self):
        self.manager.fail = True
        self.assertFalse(host.status(self.host)['host_startup_registered'])
        self.assertTrue(host.remove(self.host)['removed'])
        self.assertEqual(self.manager.calls, [])

    def test_first_private_write_loss_cannot_create_units_and_resumes_original_selection(self):
        original = store.Store.write_all; once = False
        def partial(fd, raw):
            nonlocal once
            if not once and adapter.FORMAT.encode() in raw:
                once = True; os.write(fd, raw[:17]); raise OSError('injected trigger intent loss')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=partial), self.assertRaises(OSError):
            self.install_trigger()
        self.assertTrue(once); self.assertEqual(list(self.unit_roots['runtime'].iterdir()), [])
        self.assertIsNone(host.status(self.host)['host_startup_registered'])
        self.assertTrue(self.install_trigger()['registered'])

    def test_partial_unit_copy_resumes_without_replacing_first_published_file(self):
        original = adapter.write_unit; first = None
        def partial(path, raw):
            nonlocal first
            original(path, raw)
            if first is None: first = path; raise OSError('injected first unit receipt loss')
        with mock.patch.object(adapter, 'write_unit', side_effect=partial), self.assertRaises(OSError):
            self.install_trigger()
        before = first.stat().st_ino; original_record = self.record()
        self.assertEqual(original_record['phase'], 'prepared')
        self.install_trigger(); self.assertEqual(first.stat().st_ino, before)
        self.assertEqual(self.record()['host'], original_record['host'])

    def test_ready_receipt_loss_keeps_permanent_binding_and_never_enables_timer(self):
        original = adapter.commit
        def partial(directory, value, installed):
            if value['phase'] == 'registered': raise OSError('injected registered receipt loss')
            return original(directory, value, installed)
        with mock.patch.object(adapter, 'commit', side_effect=partial), self.assertRaises(OSError):
            self.install_trigger()
        self.assertEqual(self.record()['phase'], 'prepared')
        self.assertFalse(any('enable' in c or 'start' in c for c in self.manager.calls))
        self.assertTrue(self.install_trigger()['registered'])

    def test_missing_published_history_stays_unknown_even_after_unit_removal(self):
        self.install_trigger(); adapter.remove(self.host)
        (self.host/'admin'/adapter.RECORD).unlink()
        self.assertEqual(list(self.unit_roots['runtime'].iterdir()), [])
        with self.assertRaisesRegex(ValueError, 'authority is missing'): self.install_trigger()
        self.assertIsNone(host.status(self.host)['host_startup_registered'])
        with self.assertRaisesRegex(ValueError, 'optional host trigger'): host.remove(self.host)

    def test_foreign_same_name_unit_cannot_be_claimed_or_overwritten(self):
        name = adapter.names(host.load(self.host))[0]; path = self.unit_roots['runtime']/name
        path.write_bytes(b'owned by another test fixture\n'); path.chmod(0o644)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'ownership intent'): self.install_trigger()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.host/'admin'/adapter.RECORD).exists())

    def test_changed_unit_or_loaded_override_cannot_enable_or_be_removed(self):
        self.install_trigger(); service, timer = adapter.names(host.load(self.host))
        path = self.unit_roots['runtime']/service; original = path.read_bytes()
        path.write_bytes(original+b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'digest differs'): adapter.configure(self.host, enabled=True)
        path.write_bytes(original)
        self.manager.overrides[timer] = dict(DropInPaths='/run/other-unit-override.conf')
        with self.assertRaisesRegex(ValueError, 'override changed'): adapter.remove(self.host)
        self.assertEqual(self.record()['phase'], 'removing'); self.assertFalse(self.record()['enabled'])
        self.assertTrue(path.exists()); self.assertFalse(any('stop' in c for c in self.manager.calls))

    def test_disable_is_durable_when_manager_fails_and_old_trigger_cannot_restart(self):
        self.install_trigger(); adapter.configure(self.host, enabled=True); self.helper('running')
        before = self.state_files(); self.manager.fail = True
        with self.assertRaises(OSError): adapter.configure(self.host, enabled=False)
        self.assertFalse(self.record()['enabled'])
        result = self.trigger_entry(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'], 'trigger_disabled')
        self.assertEqual(before, self.state_files())
        self.assertFalse(adapter.status(self.host)['manager_verified'])

    def test_active_helper_delays_removal_without_stopping_native_core(self):
        self.install_trigger(); adapter.configure(self.host, enabled=True)
        service, timer = adapter.names(host.load(self.host)); self.manager.active[service] = True
        before = self.state_files()
        with self.assertRaisesRegex(ValueError, 'helper is active'): adapter.remove(self.host)
        self.assertFalse(self.manager.active[timer]); self.assertTrue(self.manager.active[service])
        self.manager.active[service] = False
        result = adapter.remove(self.host); self.assertTrue(result['removed'])
        self.assertEqual(result, adapter.remove(self.host)); self.assertEqual(before, self.state_files())

    def test_partial_unit_unlink_resumes_with_original_binding(self):
        self.install_trigger(); original = Path.unlink; once = False
        def partial(path, *args, **kwargs):
            nonlocal once
            result = original(path, *args, **kwargs)
            if not once and path.parent == self.unit_roots['runtime']:
                once = True; raise OSError('injected unit unlink receipt loss')
            return result
        with mock.patch.object(Path, 'unlink', partial), self.assertRaises(OSError): adapter.remove(self.host)
        self.assertEqual(self.record()['phase'], 'removing')
        self.assertTrue(adapter.remove(self.host)['removed'])
        self.assertEqual(list(self.unit_roots['runtime'].iterdir()), [])

    def test_explicit_repair_restores_only_missing_exact_owned_unit(self):
        self.install_trigger(); before = self.record(); service, _ = adapter.names(host.load(self.host))
        path = self.unit_roots['runtime']/service; raw = path.read_bytes(); path.unlink()
        self.install_trigger(); self.assertEqual(path.read_bytes(), raw); self.assertEqual(self.record(), before)

    def test_missing_timer_file_still_withdraws_loaded_trigger(self):
        self.install_trigger(); adapter.configure(self.host, enabled=True)
        _, timer = adapter.names(host.load(self.host)); path = self.unit_roots['runtime']/timer
        path.unlink()
        self.manager.overrides[timer] = dict(LoadState='loaded', FragmentPath=str(path))
        self.assertTrue(self.manager.active[timer])
        self.assertTrue(adapter.remove(self.host)['removed'])
        self.assertFalse(self.manager.active[timer])
        self.assertIn(('--runtime', 'disable', timer), self.manager.calls)

    def test_native_entry_uninstall_requires_trigger_removal_and_preserves_history(self):
        self.install_trigger()
        with self.assertRaisesRegex(ValueError, 'optional host trigger'): host.remove(self.host)
        self.assertEqual(host.load(self.host)['phase'], 'revoking')
        self.assertTrue(adapter.remove(self.host)['removed'])
        self.assertTrue(host.remove(self.host)['removed'])
        self.assertTrue((self.host/'admin'/adapter.BOUND).exists())


if __name__ == '__main__': unittest.main(module=None)
