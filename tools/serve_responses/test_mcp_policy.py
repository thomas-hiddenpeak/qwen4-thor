"""Native MCP policy parser and packaged startup; CPU fixtures, no model."""
import json
from pathlib import Path
import subprocess
import unittest

from tools.serve_service import package as pkg
from tools.serve_service.test_service import PackageTests
from tools.serve_responses.test_file_input_policy import (
    BINARY, FileInputPolicyPackage)


class McpPolicyPackage(unittest.TestCase):
    fixture = PackageTests.fixture
    setUp = FileInputPolicyPackage.setUp

    def policy(self, **overrides):
        value = dict(version=1,
            fetch=dict(public_networks=False, allow_http=False,
                allowed_hosts=['private.example'], allowed_networks=['10.2.0.0/16'],
                ca_file='/private/not-opened.pem', ports=[443]),
            targets=[dict(kind='connector_id', id='connector_googledrive',
                url='https://private.example/mcp?private=value',
                generation='reviewed-one', scopes=['client:owner'])])
        value.update(overrides)
        path = self.root/'mcp-policy.json'
        pkg.save(path, value)
        return path

    def native_fixture(self):
        binary, release, model = FileInputPolicyPackage.native_fixture(self)
        meta = pkg.read(release)
        path = Path(meta['release_declarations']['directory'])/'capabilities.json'
        caps = pkg.read(path)
        caps['mcp_deployment'] = dict(policy_profile='q4t-mcp-deployment-policy-v1')
        pkg.save(path, caps)
        meta['release_declarations']['files']['capabilities.json'] = pkg.sha(path)
        pkg.save(release, meta)
        return binary, release, model

    def test_actual_native_checker_and_package_binding_redact_private_routes(self):
        path = self.policy()
        result = subprocess.run([str(BINARY), 'mcp-policy-check', str(path)],
            capture_output=True, timeout=10, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value['sha256'], pkg.sha(path))
        self.assertEqual(value['target_count'], 1)
        self.assertFalse(value['request_override'])
        self.assertFalse(value['redirects'])
        self.assertEqual(pkg.mcp_policy_binding(BINARY, path, path.read_bytes()), value)
        for hidden in ('private.example', '/private/', 'client:owner', 'private=value'):
            self.assertNotIn(hidden, result.stdout.decode())

    def test_invalid_and_changed_policy_never_fall_back(self):
        path = self.policy(targets=[dict(kind='connector_id', id='connector_gmail')])
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.mcp_policy_binding(BINARY, path, path.read_bytes())
        path = self.policy()
        original = path.read_bytes()
        self.policy(targets=[])
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            pkg.mcp_policy_binding(BINARY, path, original)

    def test_sealed_package_keeps_routes_after_source_removal_and_rejects_changes(self):
        binary, release, model = self.native_fixture()
        policy = self.policy()
        digest = pkg.sha(policy)
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              mcp_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        binary.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['mcp_policy']['sha256'], digest)
        command = pkg.command(target, manifest)
        self.assertEqual(command[command.index('--mcp-policy-file')+1],
                         str(target/'mcp-policy.json'))
        self.assertNotIn('--file-input-policy-file', command)
        sealed = target/'mcp-policy.json'
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o400)
        sealed.chmod(0o600)
        with sealed.open('ab') as output:
            output.write(b'\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_old_engine_cannot_claim_mcp_policy_support(self):
        binary, release, model = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'lacks native MCP deployment policy'):
            pkg.prepare(binary, release, model, self.root/'package',
                        mcp_policy=self.policy())
        self.assertFalse((self.root/'package').exists())

    def test_unconfigured_package_preserves_existing_startup(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertNotIn('mcp_policy', manifest)
        self.assertNotIn('--mcp-policy-file', pkg.command(target, manifest))
        self.assertFalse((target/'mcp-policy.json').exists())


if __name__ == '__main__':
    unittest.main()
