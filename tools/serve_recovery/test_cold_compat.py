"""A cold-awareness backport preserves the old controller and real root gate."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import cold_compat as c, controller, service
from tools.serve_recovery import test_cold_admission, test_profile_deployment


def legacy():
    raw = Path(controller.__file__).read_bytes()
    return raw.replace(c.COLD.encode(), b'').replace(c.NEW_GUARD.encode(), c.OLD_GUARD.encode())


class AdapterTests(unittest.TestCase):
    def test_backported_real_preflight_preserves_fact_and_requires_root_then_probe(self):
        fixture = test_cold_admission.ControllerTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        namespace = dict(__name__='cpu_retained_controller', __file__=controller.__file__)
        exec(compile(c.adapted(legacy()), controller.__file__, 'exec'), namespace)
        actor = fixture.actor; actor.__class__ = namespace['Controller']
        self.assertIsNone(actor.preflight())
        self.assertEqual(actor.reason, 'probe_not_authorized')
        self.assertFalse(actor.continuous)
        fixture.probe()
        self.assertTrue(actor.preflight()['device_usable'])
        self.assertFalse(actor.continuous)
        self.assertTrue(actor.probe_passed)
        self.assertEqual(actor.transaction.call_args.args[0], 'probe')

    def test_extra_legacy_hint_use_and_nonreviewed_shape_require_review(self):
        for raw in (legacy().replace(b'    def preflight(self):',
                    b'    def foreign(self): return self.continuous\n\n    def preflight(self):'),
                    legacy().replace(c.OLD_GUARD.encode(), b'            if True:\n'),
                    c.adapted(legacy())):
            with self.assertRaises((ValueError, SyntaxError)): c.adapted(raw)

    def test_sealed_adapter_changes_only_controller_and_existing_profile_shim(self):
        base = test_profile_deployment.DeploymentTests()
        base.setUp(); self.addCleanup(base.doCleanups)
        source = base.old/c.SOURCE; source.chmod(0o600); source.write_bytes(legacy()); source.chmod(0o400)
        manifest = c.engine.read(base.old/'manifest.json')
        manifest['files'][c.SOURCE] = c.engine.sha(source)
        c.engine.save(base.old/'manifest.json', manifest)
        old_sha = c.engine.sha(base.old/'manifest.json')
        (base.old/'bin/q4t').chmod(0o700)
        (base.old/'bin/q4t').write_bytes(c.bundle.launcher(old_sha, manifest['python']['path']))
        (base.old/'bin/q4t').chmod(0o500)
        old_bytes = source.read_bytes()
        receipt = c.prepare(base.old, old_sha, base.fixture.directory/'cold-adapter')
        target = Path(receipt['payload'])
        self.assertTrue(c.aware(target)); self.assertEqual(source.read_bytes(), old_bytes)
        self.assertEqual((target/c.SOURCE).read_bytes(), c.adapted(old_bytes))
        self.assertFalse(receipt['business_acceptance'])
        changed = target/'source/tools/serve_recovery/monitor.py'
        changed.chmod(0o600); changed.write_bytes(changed.read_bytes()+b'\n# unreviewed change\n')
        changed.chmod(0o400)
        value = c.engine.read(target/'manifest.json')
        value['files']['source/tools/serve_recovery/monitor.py'] = c.engine.sha(changed)
        c.engine.save(target/'manifest.json', value)
        digest = c.engine.sha(target/'manifest.json')
        (target/'bin/q4t').chmod(0o700)
        (target/'bin/q4t').write_bytes(c.bundle.launcher(digest, value['python']['path']))
        (target/'bin/q4t').chmod(0o500)
        with self.assertRaisesRegex(ValueError, 'another payload contract'):
            c.verify(base.old, old_sha, target, digest)

    def test_cli_adaptation_requires_no_live_state_or_model_action(self):
        result = dict(model_actions=False, business_acceptance=False)
        out = io.StringIO()
        with mock.patch.object(c, 'prepare', return_value=result) as call, redirect_stdout(out):
            rc = service.main(['version-adapt', '--package', '/retained',
                '--manifest-sha256', 'a'*64, '--output', '/adapter'])
        self.assertEqual(rc, 0)
        call.assert_called_once_with(Path('/retained'), 'a'*64, Path('/adapter'))
        self.assertEqual(json.loads(out.getvalue()), result)


if __name__ == '__main__': unittest.main()
