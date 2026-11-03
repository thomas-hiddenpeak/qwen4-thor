"""Client policy/package contracts. Real nginx syntax; no listeners or model."""
import copy
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest import mock

from tools.serve_gateway import client_policy as cp, gateway as g, managed as m
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_service.test_service import PackageTests


def policy():
    return dict(version=1, gateway_token=secrets.token_hex(32), clients=[
        dict(id=name, max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=4096, max_output_tokens=128, max_body_bytes=4096)
        for name in ('alpha', 'beta')])


class Clients(unittest.TestCase):
    def setUp(self):
        parent = g.ROOT/'.q4t-work/serve-clients-20261104/package-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        # Preserve private evidence even after an assertion or nginx error.
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.p = policy()
        self.path = self.root/'clients.json'
        self.path.write_text(json.dumps(self.p))
        self.path.chmod(0o600)

    fixture = PackageTests.fixture

    def test_duplicate_and_ambiguous_fields_are_rejected(self):
        raw, parsed = cp.read(self.path)
        self.assertEqual(parsed, self.p)
        for wrong in (raw.replace(b'"version": 1', b'"version": 1.0'),
                      raw.replace(b'"version": 1', b'"version": true'),
                      raw.replace(b'"version": 1', b'"version": 1, "version": 1'),
                      raw.replace(b'"max_inflight": 1', b'"max_inflight": 0'),
                      raw.replace(b'"max_inflight": 1', b'"max_inflight": 1e0'),
                      raw.replace(b'"max_inflight": 1', b'"max_inflight": NaN'),
                      b' '*(cp.MAX_BYTES+1), b'{', b'\xff'):
            with self.assertRaises(ValueError):
                cp.parse(wrong)
        for field, value in [('id', 'Alpha'), ('max_output_tokens', True),
                             ('max_body_bytes', 1023), ('max_control_inflight', 65)]:
            p = copy.deepcopy(self.p)
            p['clients'][0][field] = value
            with self.assertRaises(ValueError):
                cp.validate(p)
        p = copy.deepcopy(self.p)
        p['clients'][1]['id'] = 'alpha'
        with self.assertRaises(ValueError):
            cp.validate(p)

    def test_private_file_and_raw_digest_binding(self):
        raw, _ = cp.read(self.path)
        self.assertNotEqual(cp.binding(raw), cp.binding(raw+b'\n'))
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            cp.read(self.path)
        self.path.chmod(0o600)
        link = self.root/'link'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            cp.read(link)
        os.link(self.path, self.root/'hard')
        with self.assertRaises(ValueError):
            cp.read(self.path)
        fifo = self.root/'fifo'
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError):
            cp.read(fifo)

    def test_gateway_identity_overwrites_untrusted_headers(self):
        config = g.render(example(), self.root, self.p)
        self.assertIn('proxy_pass_request_headers off;', config)
        self.assertIn('proxy_set_header X-Q4T-Client-Id $q4t_client;', config)
        self.assertIn('proxy_set_header X-Q4T-Gateway-Token '+
                      self.p['gateway_token']+';', config)
        self.assertNotIn('$http_x_q4t_client_id', config)
        self.assertNotIn('$http_x_q4t_gateway_token', config)
        self.assertNotIn('proxy_set_header X-Q4T-Client-Id', g.render(example(), self.root))
        different = copy.deepcopy(self.p)
        different['clients'][0]['id'] = 'other'
        with self.assertRaises(ValueError):
            g.render(example(), self.root, different)

    def test_gateway_scoped_package_real_nginx_syntax_and_pinning(self):
        cert, key = certificate(self.root)
        front = self.root/'gateway.json'
        frontend = example()
        front.write_text(json.dumps(frontend))
        front.chmod(0o600)
        receipt = g.prepare(front, pkg.read(VENDOR)['binary'], cert, key,
                            self.root/'gateway', self.path)
        package = Path(receipt['package'])
        manifest = g.check(package, receipt['manifest_sha256'])
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['client_policy'], cp.binding(self.path.read_bytes()))
        self.assertNotIn(self.p['gateway_token'], (package/'manifest.json').read_text())
        for client in frontend['clients']:
            self.assertNotIn(client['api_key'], (package/'manifest.json').read_text())
        self.path.unlink()
        g.check(package, receipt['manifest_sha256'])
        with (package/'client-policy.json').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaises(ValueError):
            g.check(package, receipt['manifest_sha256'])

    def test_managed_gateway_binds_policy_parser_in_snapshot(self):
        cert, key = certificate(self.root)
        front = self.root/'gateway.json'
        front.write_text(json.dumps(example()))
        front.chmod(0o600)
        receipt = m.prepare(front, pkg.read(VENDOR)['binary'], cert, key,
                            self.root/'managed', self.path)
        manifest, gateway = m.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(gateway['version'], 2)
        self.assertIn('tools/serve_gateway/client_policy.py', manifest['sources'])

    def test_managed_backend_v4_policy_is_copied_and_verified(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              client_policy=self.path)
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['version'], 4)
        self.assertEqual(manifest['client_policy'], cp.binding(self.path.read_bytes()))
        command = pkg.command(receipt['package'], manifest)
        self.assertIn('--client-policy-file', command)
        self.assertNotIn(self.p['gateway_token'], ' '.join(command))
        self.path.unlink()
        pkg.verify(receipt['package'], receipt['manifest_sha256'])
        copied = Path(receipt['package'])/'client-policy.json'
        copied.chmod(0o600)
        copied.write_bytes(copied.read_bytes()+b'\n')
        with self.assertRaises(RuntimeError):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_effective_backend_binding_cannot_be_absent_or_different(self):
        expected = cp.binding(self.path.read_bytes())
        observed = dict(expected, profile='q4t-gateway-clients-v1', mutable=False,
                        credential_values_redacted=True)
        probe = dict(process=dict(executable_sha256='elf', listener_scope='loopback'),
                     protocol='wire', configuration=sup.expected_configuration(),
                     metrics={'q4t_gpu_healthy': 1}, ready=True, client_policy=observed)
        self.assertEqual(sup.classify(probe, 'wire', 'elf', expected), 'READY')
        for bad in (None, dict(observed, sha256='b'*64), dict(observed, enabled=False),
                    dict(observed, clients=3), dict(observed, clients=True),
                    dict(observed, mutable=True)):
            with self.assertRaises(ValueError):
                sup.classify(dict(probe, client_policy=bad), 'wire', 'elf', expected)
        cp.observed_matches(None, None)
        with self.assertRaises(ValueError):
            cp.observed_matches(observed, None)

    def test_gateway_checks_matching_backend_manifest_and_actual_policy(self):
        from tools.serve_service import service
        from tools.serve_ops import identity
        expected = cp.binding(self.path.read_bytes())
        owner = dict(pid=999, start_ticks=123, boot_id='fixture')
        status = dict(controller_live=True, state='READY', worker=dict(backend=owner))
        control = dict(package='fixture', manifest_sha256='a'*64)
        manifest = dict(settings=dict(port=18124), files={'q4t': 'b'*64},
                        client_policy=expected)
        gateway = dict(policy=dict(upstream_port=18124), client_policy=expected)
        observed = dict(expected, profile='q4t-gateway-clients-v1', mutable=False,
                        credential_values_redacted=True)
        with mock.patch.object(service, 'status', return_value=status), \
             mock.patch.object(pkg, 'read', return_value=control), \
             mock.patch.object(pkg, 'verify', return_value=manifest), \
             mock.patch.object(sup, 'process_identity', return_value=owner), \
             mock.patch.object(identity, 'process', return_value=dict(
                 listener_scope='loopback', executable_sha256='b'*64, listener_inode=123)), \
             mock.patch.object(sup, 'probe', return_value=dict(client_policy=observed)):
            self.assertEqual(g.check_backend(self.root, gateway)['backend'], owner)
            observed['sha256'] = 'c'*64
            with self.assertRaises(ValueError):
                g.check_backend(self.root, gateway)
            with self.assertRaisesRegex(ValueError, 'differs'):
                g.check_backend(self.root, dict(policy=dict(upstream_port=18124)))


if __name__ == '__main__':
    unittest.main()
