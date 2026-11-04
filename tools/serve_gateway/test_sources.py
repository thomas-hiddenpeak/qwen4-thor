"""Source-policy CPU contracts, real nginx syntax and isolated package entries."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

from tools.serve_gateway import gateway as g, managed as m
from tools.serve_gateway.test_gateway import certificate, example, VENDOR


class Sources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = g.ROOT/'build/gateway-source-tests'
        parent.mkdir(exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(dir=parent))
        cls.cert, cls.key = certificate(cls.root)
        cls.nginx = json.loads(VENDOR.read_text())['binary']

    def setUp(self):
        self.folder = self.root/secrets.token_hex(6)
        self.folder.mkdir(mode=0o700)
        self.p = dict(example(), version=2, allowed_client_cidrs=['127.0.0.1/32'])

    def prepare(self, managed=False):
        path = self.folder/'policy.json'
        g.save(path, json.dumps(self.p).encode())
        fn = m.prepare if managed else g.prepare
        return fn(path, self.nginx, self.cert, self.key, self.folder/'package',
                  trusted_ca=self.cert)

    def test_required_explicit_policy_and_no_legacy_silent_acceptance(self):
        for p in (dict(example(), version=2),
                  dict(example(), allowed_client_cidrs=['127.0.0.1/32']),
                  dict(self.p, version=3), dict(self.p, version=True)):
            with self.subTest(p=p['version']), self.assertRaises(ValueError):
                g.policy(p)

    def test_canonical_networks_only(self):
        for value in ('10.0.0.1/24', '10.0.0.0', '10.0.0.0/255.255.255.0',
                      '10.0.0.0/024', '010.0.0.0/8', '10.0/16', '10.0.0.0/33',
                      '10.0.0.0/8\n', '10.0.0.0/8;include bad', 'localhost/32',
                      '::1/128', '::ffff:127.0.0.1/128', True, 167772161, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                g.policy(dict(self.p, allowed_client_cidrs=[value]))

    def test_only_explicit_private_ranges_and_loopback(self):
        for net in ('0.0.0.0/0', '8.8.8.8/32', '169.254.0.0/16',
                    '100.64.0.0/10', '172.0.0.0/8', '192.168.0.0/15',
                    '224.0.0.0/4', '255.255.255.255/32', '126.0.0.0/7'):
            with self.subTest(net=net), self.assertRaises(ValueError):
                g.policy(dict(self.p, allowed_client_cidrs=[net]))
        accepted = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                    '127.0.0.0/8']
        self.assertEqual(g.policy(dict(self.p, allowed_client_cidrs=accepted))[
            'allowed_client_cidrs'], accepted)

    def test_bounded_nonempty_array_and_no_overlap(self):
        for nets in ([], None, '127.0.0.1/32', {}, ['10.0.0.0/8']*2,
                     ['10.0.0.0/8', '10.1.0.0/16'],
                     ['10.1.0.0/16', '10.0.0.0/8'],
                     [f'10.0.0.{i}/32' for i in range(33)]):
            with self.subTest(nets=nets), self.assertRaises(ValueError):
                g.policy(dict(self.p, allowed_client_cidrs=nets))
        nets = [f'10.0.0.{i}/32' for i in range(32)]
        result = g.policy(dict(self.p, allowed_client_cidrs=nets))
        self.assertEqual(result['allowed_client_cidrs'], nets)
        self.assertIsNot(result['allowed_client_cidrs'], nets)

    def test_new_lan_v1_rejected_before_material_or_output(self):
        self.p = dict(example(), listen_host='10.1.2.3')
        # Legacy parsing remains possible; new rendering/preparation is gated.
        g.policy(self.p)
        with self.assertRaisesRegex(ValueError, 'require policy v2'):
            g.render(self.p, self.folder)
        with self.assertRaisesRegex(ValueError, 'require policy v2'):
            self.prepare()
        self.assertFalse((self.folder/'package').exists())

    def test_real_nginx_syntax_for_lan_and_loopback(self):
        self.p['allowed_client_cidrs'] = [
            '10.1.0.0/16', '192.168.2.3/32', '127.0.0.1/32']
        receipt = self.prepare()
        manifest = g.check(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['policy']['allowed_client_cidrs'],
                         self.p['allowed_client_cidrs'])
        self.assertFalse((Path(receipt['package'])/'runtime/nginx.pid').exists())
        # nginx -t also binds the configured address. Unassigned LAN addresses
        # must not be installed on this machine merely to test configuration.
        for host in ('127.0.0.1', '10.1.2.3', '172.16.0.2', '192.168.2.3'):
            raw = g.render(dict(self.p, listen_host=host), self.folder)
            self.assertIn(f'listen {host}:18443 ssl default_server;', raw)
            self.assertIn('192.168.2.3/32 1;', raw)

    def test_source_precedes_auth_route_and_proxy_without_forwarded_trust(self):
        raw = g.render(self.p, self.folder)
        self.assertIn('geo $remote_addr $q4t_source_allowed {\n    default 0;', raw)
        check = raw.index('if ($q4t_source_allowed = 0) { return 403; }')
        for marker in ('if ($host', 'if ($ssl_server_name', 'if ($q4t_client',
                       'if ($q4t_lane', 'proxy_pass http:'):
            self.assertLess(check, raw.index(marker))
        for forbidden in ('real_ip_header', 'set_real_ip_from', 'proxy_protocol',
                          'proxy_recursive', '$http_x_forwarded_for', 'proxy 127.'):
            self.assertNotIn(forbidden, raw)
        self.assertIn('gateway_source_denied', raw)
        self.assertIn('"source_allowed":$q4t_source_allowed', raw)
        self.assertNotIn('"source_ip"', raw)
        self.assertIn('proxy_pass_request_headers off;', raw)

    def test_no_implicit_loopback_allowance_or_key_exemption(self):
        raw = g.render(dict(self.p, allowed_client_cidrs=['10.1.2.0/24']),
                       self.folder)
        geo = raw.split('geo $remote_addr', 1)[1].split('}', 1)[0]
        self.assertNotIn('127.', geo)
        self.assertNotIn('$q4t_client', geo)
        self.assertNotIn('default 1;', geo)

    def test_manifest_is_secret_free_and_cidr_changes_require_a_new_package(self):
        receipt = self.prepare()
        package = Path(receipt['package'])
        raw = (package/'manifest.json').read_text()
        for client in self.p['clients']:
            self.assertNotIn(client['api_key'], raw)
        self.assertNotIn('PRIVATE KEY', raw)
        config = package/'nginx.conf'
        config.write_text(config.read_text().replace('127.0.0.1/32 1;',
                                                    '127.0.0.0/8 1;'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            g.check(package, receipt['manifest_sha256'])

    def test_managed_snapshot_and_tls_observer_accept_new_policy(self):
        from tools.serve_tls import source
        receipt = self.prepare(managed=True)
        outer, inner = m.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual((outer['version'], inner['version'],
                          inner['policy']['version']), (4, 3, 2))
        observed = source.package(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(observed['listen_host'], '127.0.0.1')
        entry = Path(receipt['package'])/'source/tools/serve_gateway/gateway.py'
        result = subprocess.run([sys.executable, '-I', '-B', str(entry), 'check',
            '--package', inner['nginx']['path'].rsplit('/', 1)[0]+'/gateway',
            '--manifest-sha256', outer['gateway']['manifest_sha256']],
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(outer['automatic_start'])
        self.assertFalse(inner['production_deployment_accepted'])

    def test_previous_fixed_package_still_uses_its_original_entry(self):
        old = g.parse((g.ROOT/'.q4t-work/serve-tls-observation-20261104/plan.json').read_bytes())
        selected = old['gateways'][0]
        entry = Path(selected['package'])/'source/tools/serve_gateway/managed.py'
        result = subprocess.run([sys.executable, '-I', '-B', str(entry), 'unit',
            '--package', selected['package'], '--manifest-sha256', selected['manifest_sha256'],
            '--backend-run', str(self.folder/'not-started'),
            '--output', str(self.folder/'not-started-front')],
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b'Restart=no', result.stdout)
        self.assertFalse((self.folder/'not-started-front').exists())


if __name__ == '__main__':
    unittest.main()
