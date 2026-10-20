"""Scope-specific evidence cannot borrow the historical service PID contract."""
import json
import unittest

import audit_offload_matrix as audit
import test_offload_matrix_audit as fixtures


class ScopeEvidence(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MatrixFixture()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.directory = self.f.off
        self.protocol = json.loads((self.directory / 'protocol.json').read_text())
        self.protocol['unit'] = 'q4t-test.scope'
        self.wrapper = json.loads((self.directory / 'wrapper-exit.json').read_text())
        self.iso = self.directory / 'http/isolation'
        identity = json.loads((self.iso / 'identity.json').read_text())
        identity.update(kind='scope', token='q4t-scope-' + 'a' * 32, start_ticks=1234)
        identity['properties'].pop('MainPID')
        identity['properties'].update(ControlGroup='/system.slice/q4t-test.scope',
                                      Description=identity['token'])
        identity['membership'] = ['0::/system.slice/q4t-test.scope']
        self.f.save(self.iso / 'identity.json', identity)
        self.f.save(self.iso / 'payload.json', {key:identity[key] for key in
            ('pid', 'token', 'start_ticks', 'membership')})
        self.f.save(self.iso / 'payload-exit.json', dict(token=identity['token'],returncode=0))
        self.f.save(self.iso / 'cleanup.json', dict(stop_rc=0,unit_removed=True,
                    properties_after={'LoadState':'not-found'}))

    def check(self):
        return audit.check_processes(audit.Evidence(), self.directory, self.protocol, self.wrapper)

    def test_scope_does_not_require_fabricated_main_pid(self):
        self.check()

    def test_scope_rejects_other_owner(self):
        self.f.edit(self.iso/'identity.json',lambda value:value['properties'].update(Description='foreign'))
        with self.assertRaisesRegex(ValueError,'scope payload identity'):
            self.check()

    def test_scope_rejects_failed_payload(self):
        self.f.edit(self.iso/'payload-exit.json',lambda value:value.update(returncode=-9))
        with self.assertRaisesRegex(ValueError,'scope payload identity'):
            self.check()

    def test_scope_rejects_reused_pid(self):
        self.f.edit(self.iso/'payload.json',lambda value:value.update(start_ticks=9999))
        with self.assertRaisesRegex(ValueError,'scope payload identity'):
            self.check()


if __name__ == '__main__':
    unittest.main()
