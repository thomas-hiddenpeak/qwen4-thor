"""Selected lifecycle gates and the exact 11h reader, without listeners."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from tools.project_tasks import operations_stage as s
from tools.project_tasks import protocol_driver_cpu as old
from tools.project_tasks import transport, test_errors, test_tasks


class Driver(old.Driver):
    def test_actual_11h_profile_boundary_and_binding(self):
        output, _ = self.fixture()
        runtime = output/'historical'
        s.history.extract_runtime(7, runtime)
        private = runtime/'.q4t-work/private'
        private.mkdir(mode=0o700, parents=True)
        ca = private/'ca.pem'
        ca.write_bytes(test_errors.CA.read_bytes())
        value = {k:v for k,v in test_tasks.BINDING.items()
                 if k not in ('ca_sha256', 'key_sha256')}
        value.update(api_key='cpu-profile-boundary-'+'x'*40, ca_file=str(ca),
                     protocol_sha256=s.review.BEFORE, template_sha256=s.review.TEMPLATE)
        profile = private/'profile.json'
        s.pkg.save(profile, value)
        expected = transport.profile(profile)[1]
        script = 'from tools.project_tasks import transport;import json,sys;print(json.dumps(transport.profile(sys.argv[1])[1]))'
        child = subprocess.run([sys.executable, '-B', '-c', script, str(profile)],
            cwd=runtime, capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), expected)
        external = output/'outside.json'
        s.pkg.save(external, value)
        child = subprocess.run([sys.executable, '-B', '-c', script, str(external)],
            cwd=runtime, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn('use build or .q4t-work', child.stderr)

    def test_unreviewed_capability_change_is_rejected(self):
        output, _ = self.fixture()
        before = dict(protocol_source_sha256=s.review.BEFORE,
            observability=dict(version=4), http=dict(profile='old'), tools=dict(safe=True))
        s.pkg.save(output/'capabilities-0.json', before)
        service = Mock()
        service.request.return_value = dict(before, protocol_source_sha256=s.review.AFTER,
            observability=dict(version=5), http=dict(profile='new'), tools=dict(safe=False))
        run = s.Run(dict(versions=[dict(protocol=s.review.BEFORE), dict(protocol=s.review.AFTER)]))
        with patch.object(s, 'OUT', output), patch.object(s.prior.Run, 'launch', return_value=service):
            with self.assertRaisesRegex(RuntimeError, 'unreviewed public capability'):
                run.launch(1)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(Driver):
        if name != 'test_real_11g_profile_and_ca_must_be_inside_historical_root':
            suite.addTest(Driver(name))
    return suite
