"""Real native web policy checker and sealed package; no model execution."""
import json
from pathlib import Path
import subprocess
import unittest

from tools.serve_responses.test_file_input_policy import (
    BINARY, FileInputPolicyPackage)
from tools.serve_service import package as pkg
from tools.serve_service.test_service import PackageTests


class WebPolicyPackage(unittest.TestCase):
    fixture = PackageTests.fixture
    setUp = FileInputPolicyPackage.setUp

    def policy(self, **overrides):
        value = dict(version=1, seeds=['https://private.example/records'],
            scopes=['client:private-owner'], max_pages=2, max_depth=0,
            fetch=dict(public_networks=False, allow_http=False,
                allowed_hosts=['private.example'], allowed_networks=['10.2.0.0/16'],
                ca_file='/private/not-opened.pem', ports=[443]))
        value.update(overrides)
        path = self.root/'web-policy.json'
        pkg.save(path, value)
        return path

    def native_fixture(self):
        binary, release, model = FileInputPolicyPackage.native_fixture(self)
        meta = pkg.read(release)
        path = Path(meta['release_declarations']['directory'])/'capabilities.json'
        caps = pkg.read(path)
        caps['web_search'] = dict(policy_profile='q4t-web-deployment-policy-v1')
        pkg.save(path, caps)
        meta['release_declarations']['files']['capabilities.json'] = pkg.sha(path)
        pkg.save(release, meta)
        return binary, release, model

    def test_native_checker_binds_exact_bytes_and_redacts(self):
        path = self.policy()
        result = subprocess.run([str(BINARY), 'web-policy-check', str(path)],
            capture_output=True, timeout=10, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value['source_sha256'], pkg.sha(path))
        self.assertEqual(value['backend'], 'native_configured_sites')
        self.assertEqual((value['seed_count'], value['scope_count']), (1, 1))
        self.assertFalse(value['request_override'])
        self.assertFalse(value['core_readiness_dependency'])
        self.assertFalse(value['model_restart_on_error'])
        self.assertEqual(pkg.web_policy_binding(BINARY, path, path.read_bytes()), value)
        for secret in ('private.example', '/private/', '10.2.0.0', 'private-owner'):
            self.assertNotIn(secret, result.stdout.decode())

    def test_provider_seals_private_endpoint_only_with_matching_capabilities(self):
        path = self.policy()
        value = pkg.read(path)
        del value['seeds']
        value['provider'] = dict(type='mwmbl_v2', endpoint='https://private.example/search')
        pkg.save(path, value)
        binary, release, model = self.native_fixture()
        target = self.root/'package'
        with self.assertRaisesRegex(RuntimeError, 'lacks both Chat and Responses'):
            pkg.prepare(binary, release, model, target, web_policy=path)
        meta = pkg.read(release)
        caps_path = Path(meta['release_declarations']['directory'])/'capabilities.json'
        caps = pkg.read(caps_path)
        caps['web_search']['optional_providers'] = {'mwmbl_v2': True}
        caps.setdefault('responses', {})['web_search'] = dict(optional_providers={'mwmbl_v2': True})
        pkg.save(caps_path, caps)
        meta['release_declarations']['files']['capabilities.json'] = pkg.sha(caps_path)
        pkg.save(release, meta)
        receipt = pkg.prepare(binary, release, model, target, web_policy=path)
        path.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        binding = manifest['web_policy']
        self.assertEqual(binding['backend'], 'mwmbl_v2')
        self.assertEqual(binding['source_sha256'], pkg.sha(target/'web-policy.json'))
        self.assertNotIn('private.example', json.dumps(manifest))
        self.assertIn(str(target/'web-policy.json'), pkg.command(target, manifest))

    def test_semantic_index_identity_does_not_weaken_source_binding(self):
        path = self.policy()
        original = path.read_bytes()
        before = pkg.web_policy_binding(BINARY, path, original)
        path.write_bytes(original+b'\n')
        after = pkg.web_policy_binding(BINARY, path, path.read_bytes())
        self.assertEqual(before['sha256'], after['sha256'])
        self.assertNotEqual(before['source_sha256'], after['source_sha256'])
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            pkg.web_policy_binding(BINARY, path, original)

    def test_invalid_or_changed_policy_cannot_fall_back(self):
        path = self.policy(max_pages=0)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.web_policy_binding(BINARY, path, path.read_bytes())
        path = self.policy()
        original = path.read_bytes()
        self.policy(scopes=['client:different'])
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            pkg.web_policy_binding(BINARY, path, original)

    def test_checker_rejects_public_file_and_symlink(self):
        path = self.policy()
        raw = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.web_policy_binding(BINARY, path, raw)
        path.chmod(0o600)
        alias = self.root/'alias.json'
        alias.symlink_to(path)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.web_policy_binding(BINARY, alias, raw)

    def test_sealed_policy_survives_source_removal_and_detects_tampering(self):
        binary, release, model = self.native_fixture()
        policy = self.policy()
        raw = policy.read_bytes()
        expected = pkg.web_policy_binding(BINARY, policy, raw)
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              web_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        binary.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['web_policy'], expected)
        sealed = target/'web-policy.json'
        self.assertEqual(sealed.read_bytes(), raw)
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o400)
        command = pkg.command(target, manifest)
        self.assertEqual(command[command.index('--web-policy-file')+1], str(sealed))
        sealed.chmod(0o600)
        with sealed.open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_old_declaration_cannot_claim_web_support(self):
        binary, release, model = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'lacks native web search'):
            pkg.prepare(binary, release, model, self.root/'package',
                        web_policy=self.policy())
        self.assertFalse((self.root/'package').exists())

    def test_unconfigured_package_has_no_search_startup_dependency(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertNotIn('web_policy', manifest)
        self.assertNotIn('--web-policy-file', pkg.command(target, manifest))
        self.assertFalse((target/'web-policy.json').exists())


if __name__ == '__main__':
    unittest.main()
