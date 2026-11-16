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

    def test_native_tunnel_without_url_survives_private_package_binding(self):
        binary, release, model = self.native_fixture()
        policy = self.policy(targets=[dict(kind='tunnel_id', id='tunnel_project',
            transport='native', generation='private-generation',
            scopes=['client:owner'], agents=['client:private-agent'])])
        parsed = pkg.mcp_policy_binding(BINARY, policy, policy.read_bytes())
        self.assertEqual(parsed['native_tunnel_count'], 1)
        self.assertNotIn('private-agent', json.dumps(parsed))
        self.assertNotIn('private-generation', json.dumps(parsed))
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              mcp_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['mcp_policy']['native_tunnel_count'], 1)
        self.assertEqual(pkg.read(target/'mcp-policy.json')['targets'][0]['transport'], 'native')
        command = pkg.command(target, manifest)
        self.assertEqual(command[command.index('--mcp-policy-file')+1],
                         str(target/'mcp-policy.json'))

    def test_invalid_and_changed_policy_never_fall_back(self):
        path = self.policy(targets=[dict(kind='connector_id', id='connector_gmail')])
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.mcp_policy_binding(BINARY, path, path.read_bytes())
        path = self.policy()
        original = path.read_bytes()
        self.policy(targets=[])
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            pkg.mcp_policy_binding(BINARY, path, original)

    def test_native_provider_keeps_scope_authority_and_needs_no_external_mcp_url(self):
        binary, release, model = self.native_fixture()
        policy = self.policy(fetch=dict(public_networks=True, allow_http=False,
            ports=[443], max_redirects=0), targets=[dict(kind='connector_id',
            id='connector_googlecalendar', transport='provider',
            generation='calendar-private-generation', scopes=['client:owner'])])
        parsed = pkg.mcp_policy_binding(BINARY, policy, policy.read_bytes())
        self.assertEqual(parsed['native_provider_count'], 1)
        self.assertEqual(parsed['native_tunnel_count'], 0)
        self.assertNotIn('calendar-private-generation', json.dumps(parsed))
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              mcp_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['mcp_policy'], parsed)
        route = pkg.read(target/'mcp-policy.json')['targets'][0]
        self.assertEqual(route['transport'], 'provider')
        self.assertEqual(route['scopes'], ['client:owner'])
        self.assertNotIn('url', route)
        self.assertIn('--mcp-policy-file', pkg.command(target, manifest))

    def test_native_google_provider_set_is_sealed_with_separate_scopes(self):
        binary, release, model = self.native_fixture()
        ids = ['connector_googlecalendar', 'connector_googledrive',
               'connector_gmail']
        routes = [dict(kind='connector_id', id=provider, transport='provider',
                       generation='private-' + str(index),
                       scopes=['client:provider-' + str(index)])
                  for index, provider in enumerate(ids)]
        policy = self.policy(fetch=dict(public_networks=True, allow_http=False,
            ports=[443], max_redirects=0), targets=routes)
        parsed = pkg.mcp_policy_binding(BINARY, policy, policy.read_bytes())
        self.assertEqual(parsed['native_provider_count'], 3)
        self.assertEqual(parsed['native_tunnel_count'], 0)
        self.assertNotIn('private-', json.dumps(parsed))
        receipt = pkg.prepare(binary, release, model, self.root/'google-package',
                              mcp_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['mcp_policy'], parsed)
        self.assertEqual(pkg.read(target/'mcp-policy.json')['targets'], routes)
        self.assertIn('--mcp-policy-file', pkg.command(target, manifest))

    def test_native_graph_provider_set_is_sealed_with_separate_scopes(self):
        binary, release, model = self.native_fixture()
        ids = ['connector_outlookcalendar', 'connector_outlookemail',
               'connector_microsoftteams', 'connector_sharepoint']
        routes = [dict(kind='connector_id', id=provider, transport='provider',
                       generation='graph-private-' + str(index),
                       scopes=['client:graph-' + str(index)])
                  for index, provider in enumerate(ids)]
        policy = self.policy(fetch=dict(public_networks=True, allow_http=False,
            ports=[443], max_redirects=0), targets=routes)
        parsed = pkg.mcp_policy_binding(BINARY, policy, policy.read_bytes())
        self.assertEqual(parsed['native_provider_count'], 4)
        self.assertEqual(parsed['native_tunnel_count'], 0)
        self.assertNotIn('graph-private-', json.dumps(parsed))
        receipt = pkg.prepare(binary, release, model, self.root/'graph-package',
                              mcp_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['mcp_policy'], parsed)
        self.assertEqual(pkg.read(target/'mcp-policy.json')['targets'], routes)
        self.assertIn('--mcp-policy-file', pkg.command(target, manifest))

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
