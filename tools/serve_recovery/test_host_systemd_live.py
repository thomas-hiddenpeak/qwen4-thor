"""Explicit CPU integration using one new /run-only systemd unit pair.

The model intent stays STOPPED and the fixture forbids GPU lease acquisition.
No existing host service or /etc configuration is changed. Keep root-protected
source and resource evidence if withdrawal of our exact units cannot be proved.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import host_install as host, host_systemd as adapter
from tools.serve_recovery import resource_install as resources, resource_kernel as kernel
from tools.serve_recovery import test_host_install as fixtures


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HostTests(methodName='runTest')
        self.fixture.setUp()
        self.registered = False; self.names = (); self.events = []
        self.output = Path(os.environ['Q4T_SYSTEMD_CPU_RECEIPT'])
        self.assertFalse(self.output.exists())
        self.addCleanup(self.cleanup)
        self.fixture.initialize_host()
        host.configure(self.fixture.host, enabled=True)
        self.names = adapter.names(host.load(self.fixture.host))
        self.note('prepared', host=str(self.fixture.host), units=list(self.names),
                  resource=str(self.fixture.directory), model_gpu_starts=0)

    def note(self, event, **value):
        self.events.append(dict(event=event, **value))
        self.output.write_text(json.dumps(self.events, indent=2)+'\n')
        os.chown(self.output, self.fixture.uid, self.fixture.gid)

    def wait_inactive(self, name, seconds=10):
        deadline = time.monotonic()+seconds
        while True:
            info = adapter.properties(name)
            if info['ActiveState'] in ('inactive', 'failed'): return info
            if time.monotonic() >= deadline:
                raise TimeoutError('owned helper did not finish: '+repr(info))
            time.sleep(.05)

    def cleanup(self):
        fixture = self.fixture
        try:
            if self.registered:
                installed = host.load(fixture.host)
                value = adapter.load(fixture.host, installed)
                if value['phase'] == 'registered':
                    adapter.configure(fixture.host, enabled=False)
                self.wait_inactive(self.names[0])
                adapter.remove(fixture.host)
            for name in self.names:
                self.assertFalse((Path('/run/systemd/system')/name).exists())
                self.assertFalse((Path('/etc/systemd/system')/name).exists())
                self.assertFalse(os.path.lexists(Path('/run/systemd/system/timers.target.wants')/name))
                self.assertEqual(adapter.properties(name)['LoadState'], 'not-found')
            with kernel.Scope(resources.load(fixture.directory)['record']) as scope:
                self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))
            host.remove(fixture.host)
            self.assertTrue(fixture.doCleanups())
            self.note('cleaned', owned_units_absent=True, resources_removed=True,
                      existing_host_services_changed=False)
        except BaseException:
            # Never erase an installed entry's source while a manager may
            # still invoke it. Stop the test instrumentation, preserve evidence.
            fixture.patch.stop()
            fixture.temporary._finalizer.detach()
            self.note('cleanup_unconfirmed_preserved', path=str(fixture.parent))
            raise

    def test_real_runtime_timer_checks_stopped_service_and_is_fully_removed(self):
        fixture = self.fixture; before = fixture.state_files()
        self.assertEqual(adapter.SYSTEMCTL, Path('/usr/bin/systemctl'))
        for name in self.names:
            self.assertEqual(adapter.properties(name)['LoadState'], 'not-found')
        # A partial register must also be explicitly withdrawn in cleanup.
        self.registered = True
        result = adapter.install(fixture.host, 'runtime')
        self.assertFalse(result['enabled'])
        self.note('registered', status=adapter.status(fixture.host))
        adapter.configure(fixture.host, enabled=True)
        service, timer = self.names
        deadline = time.monotonic()+10
        while True:
            raw = adapter.manager('show', service,
                '--property=ExecMainCode,ExecMainStatus,ExecMainExitTimestampMonotonic')
            properties = dict(row.split('=', 1) for row in raw.splitlines())
            if int(properties.get('ExecMainExitTimestampMonotonic', '0')) > 0: break
            if time.monotonic() >= deadline: self.fail('owned timer did not invoke native ensure')
            time.sleep(.05)
        self.assertEqual(properties['ExecMainCode'], '1')
        self.assertEqual(properties['ExecMainStatus'], '0')
        self.wait_inactive(service)
        self.assertEqual(adapter.properties(timer)['ActiveState'], 'active')
        self.assertEqual(before, fixture.state_files())
        self.assertFalse((Path(fixture.fixture['state'])/'outer-runs').exists())
        self.note('timer_completed', execution=properties, core_json_unchanged=True)
        adapter.configure(fixture.host, enabled=False)
        self.assertEqual(adapter.properties(timer)['ActiveState'], 'inactive')
        adapter.manager('start', service)
        self.wait_inactive(service)
        self.assertEqual(before, fixture.state_files())
        self.note('disabled_trigger_declined', core_json_unchanged=True)
        self.assertTrue(adapter.remove(fixture.host)['removed'])
        self.note('removed', native_intent_unchanged=True)


if __name__ == '__main__': unittest.main(module=None)
