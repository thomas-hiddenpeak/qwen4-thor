"""Exercise the native policy checker and the package executed by the service."""
import json
from pathlib import Path
import unittest

from tools.serve_responses.test_file_input_policy import BINARY, FileInputPolicyPackage
from tools.serve_service import package
from tools.serve_service.test_service import PackageTests


class WebhookPolicyPackage(unittest.TestCase):
    fixture = PackageTests.fixture
    setUp = FileInputPolicyPackage.setUp

    def policy(self):
        path = self.root/'webhook-policy.json'
        package.save(path, dict(version=1, network=dict(public_networks=False,
            allowed_hosts=['subscriber.example'], allowed_networks=['10.2.0.0/16'],
            ports=[443], timeout_ms=3000)))
        return path

    def test_sealed_policy_checked_and_started_by_the_packaged_engine(self):
        binary, release, model = FileInputPolicyPackage.native_fixture(self)
        policy = self.policy()
        raw = policy.read_bytes()
        identity = package.webhook_policy_binding(binary, policy, raw)
        self.assertEqual(identity['sha256'], package.sha(policy))
        self.assertEqual(identity['allowed_host_count'], 1)
        self.assertTrue(identity['https_only'])
        self.assertEqual(identity['redirects'], 0)
        self.assertNotIn('subscriber.example', json.dumps(identity))
        meta = package.read(release)
        declarations = Path(meta['release_declarations']['directory'])/'capabilities.json'
        capabilities = package.read(declarations)
        with self.assertRaisesRegex(RuntimeError, 'lacks native webhook'):
            package.prepare(binary, release, model, self.root/'old', webhook_policy=policy)
        capabilities['webhooks'] = dict(policy_profile=identity['profile'])
        package.save(declarations, capabilities)
        meta['release_declarations']['files']['capabilities.json'] = package.sha(declarations)
        package.save(release, meta)
        receipt = package.prepare(binary, release, model, self.root/'package', webhook_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        manifest = package.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['webhook_policy'], identity)
        sealed = target/'webhook-policy.json'
        self.assertEqual(sealed.read_bytes(), raw)
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o400)
        args = package.command(target, manifest)
        self.assertEqual(args[args.index('--webhook-policy-file')+1], str(sealed))
        sealed.chmod(0o600)
        sealed.write_bytes(raw+b'\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            package.verify(target, receipt['manifest_sha256'])

    def test_default_package_keeps_egress_optional(self):
        binary, release, model = self.fixture()
        receipt = package.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = package.verify(target, receipt['manifest_sha256'])
        self.assertNotIn('webhook_policy', manifest)
        self.assertNotIn('--webhook-policy-file', package.command(target, manifest))

    def test_policy_must_be_private_and_allow_specific_recipients(self):
        path = self.policy()
        raw = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            package.webhook_policy_binding(BINARY, path, raw)
        path.chmod(0o600)
        package.save(path, dict(version=1, network=dict(allowed_hosts=[])))
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            package.webhook_policy_binding(BINARY, path, path.read_bytes())
        path = self.policy()
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            package.webhook_policy_binding(BINARY, path, path.read_bytes()+b' ')
