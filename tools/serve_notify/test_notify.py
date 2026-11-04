"""CPU contracts and process failures; no listening sockets or GPU work."""
from contextlib import ExitStack
import copy
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.serve_notify import notify as n

BASE = n.ROOT/'.q4t-work/serve-notify-20261104'


def dependencies():
    old = n.ROOT/'.q4t-work/serve-ops-20261103/deps'
    return {name:(old if name in ('prometheus', 'promtool') else BASE/'deps')/name
            for name in n.BINARIES}


def private_inputs(root, port=19445):
    cert, key = root/'certificate.pem', root/'key.pem'
    result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
        '-nodes', '-days', '2', '-subj', '/CN=q4t-notify.test', '-addext',
        'subjectAltName=DNS:q4t-notify.test,IP:127.0.0.1',
        '-keyout', str(key), '-out', str(cert)], capture_output=True, timeout=15)
    n.require(result.returncode == 0, 'fixture certificate generation')
    key.chmod(0o600)
    token = secrets.token_urlsafe(32)
    n.m.g.save(root/'token-private', token.encode())
    value = dict(version=1, deployment='thor-acceptance', runner_port=18123,
        prometheus_port=19095, alertmanager_port=19096,
        receiver_url=f'https://127.0.0.1:{port}/alerts',
        receiver_token_file=str(root/'token-private'), receiver_ca_file=str(cert))
    n.pkg.save(root/'policy-private.json', value)
    return value


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = dict(version=1, deployment='thor', runner_port=18123,
            prometheus_port=19095, alertmanager_port=19096,
            receiver_url='https://127.0.0.1:19445/alerts',
            receiver_token_file='/private/token')

    def test_unknown_fields_types_and_port_collisions(self):
        for name, value in [('version', True), ('deployment', 'bad\nlabel'),
                ('runner_port', True), ('runner_port', 80),
                ('alertmanager_port', 19095), ('restarts', 1)]:
            with self.subTest(name=name, value=value):
                with self.assertRaises(RuntimeError): n.policy(dict(self.p, **{name:value}))
        q = dict(self.p); del q['runner_port']
        with self.assertRaises(RuntimeError): n.policy(q)

    def test_url_credentials_downgrade_and_control_rejected(self):
        for url in ['http://127.0.0.1/a', 'https://user:pass@a/b',
                    'https://a/b?key=x', 'https://a/b#x', 'https://a/',
                    'https://a/b\n', 'file:///a', 'https://a:65536/b']:
            if url == 'https://a/':
                self.assertEqual(n.policy(dict(self.p, receiver_url=url))['receiver_url'], url)
            else:
                with self.subTest(url=url), self.assertRaises((RuntimeError, ValueError)):
                    n.policy(dict(self.p, receiver_url=url))

    def test_configuration_keeps_credentials_out_and_tls_on(self):
        configs = n.configs(self.p, Path('/private/package'))
        hook = configs['alertmanager']['receivers'][0]['webhook_configs'][0]
        self.assertNotIn(self.p['receiver_url'], json.dumps(configs))
        self.assertTrue(hook['send_resolved'])
        self.assertFalse(hook['http_config']['follow_redirects'])
        self.assertFalse(hook['http_config']['proxy_from_environment'])
        self.assertFalse(hook['http_config']['tls_config']['insecure_skip_verify'])
        self.assertEqual(hook['max_alerts'], 0)
        self.assertEqual(configs['alertmanager']['route']['routes'][0]['repeat_interval'], '1m')


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = n.ROOT/'build/serve-notify-tests'; folder.mkdir(exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(dir=folder)); cls.root.chmod(0o700)
        cls.p = private_inputs(cls.root)
        cls.receipt = n.prepare(cls.root/'policy-private.json', dependencies(), cls.root/'package')
        cls.package = Path(cls.receipt['package']); cls.digest = cls.receipt['manifest_sha256']

    def setUp(self):
        self.folder = self.root/secrets.token_hex(5); self.folder.mkdir(mode=0o700)

    def test_real_validators_and_snapshot(self):
        manifest = n.verify(self.package, self.digest)
        self.assertEqual(manifest['files']['source/deploy/serve-ops/q4t.rules.yml'],
                         n.pkg.sha(n.ROOT/'deploy/serve-ops/q4t.rules.yml'))
        for name in ('notify.py', 'child.py'):
            value = subprocess.run([manifest['python']['path'], '-I', '-B',
                self.package/'source/tools/serve_notify'/name, '--help'],
                capture_output=True, timeout=15)
            self.assertEqual(value.returncode, 0, value.stderr.decode())

    def test_private_secret_and_symlink_rejected(self):
        key = self.folder/'bad'; key.write_text('a'*40); key.chmod(0o644)
        p = dict(self.p, receiver_token_file=str(key))
        path = self.folder/'policy-private.json'; n.pkg.save(path, p)
        with self.assertRaises(ValueError): n.prepare(path, dependencies(), self.folder/'must-not-exist')
        key.chmod(0o600)
        link = self.folder/'link'; link.symlink_to(key)
        n.pkg.save(path, dict(p, receiver_token_file=str(link)))
        with self.assertRaises((ValueError, OSError)):
            n.prepare(path, dependencies(), self.folder/'must-not-exist')
        self.assertFalse((self.folder/'must-not-exist').exists())

    def test_manifest_identity_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'manifest changed'):
            n.verify(self.package, '0'*64)

    def test_snapshot_tamper_rejected_without_changing_original(self):
        # Only the small manifest is copied. The verifier must reject an
        # inventory mismatch before reading any absent binary or private data.
        n.pkg.save(self.folder/'manifest.json', dict(
            n.m.read(self.package/'manifest.json'), files={}))
        with self.assertRaisesRegex(RuntimeError, 'contract'):
            n.verify(self.folder, n.pkg.sha(self.folder/'manifest.json'))

    def test_state_binding_and_no_overwrite(self):
        state = self.folder/'state'
        n.init_state(self.package, self.digest, state)
        self.assertEqual(n.check_state(state, self.package, self.digest), state)
        with self.assertRaises(FileExistsError): n.init_state(self.package, self.digest, state)
        with self.assertRaisesRegex(RuntimeError, 'another package'):
            n.check_state(state, self.package, '0'*64)

    def test_role_claims_are_exclusive_and_independent(self):
        with n.claim(self.folder, 'prometheus'), n.claim(self.folder, 'alertmanager'):
            with self.assertRaises(BlockingIOError):
                with n.claim(self.folder, 'prometheus'): pass
        with n.claim(self.folder, 'prometheus'): pass

    def test_claim_symlink_and_permissions_rejected(self):
        target = self.folder/'untouched'; target.write_text('unchanged')
        (self.folder/'prometheus.lock').symlink_to(target)
        with self.assertRaises(OSError):
            with n.claim(self.folder, 'prometheus'): pass
        target2 = self.folder/'alertmanager.lock'; target2.write_text(''); target2.chmod(0o644)
        with self.assertRaises(RuntimeError):
            with n.claim(self.folder, 'alertmanager'): pass
        self.assertEqual(target.read_text(), 'unchanged')

    def test_unit_and_commands_use_pinned_loopback_without_restart(self):
        state = self.folder/'state'; n.init_state(self.package, self.digest, state)
        manifest = n.verify(self.package, self.digest)
        for role in n.ROLES:
            text = n.unit(self.package, self.digest, role, state, self.folder/role)
            unit = self.folder/(role+'.service'); unit.write_text(text)
            result = subprocess.run(['systemd-analyze', '--user', 'verify', unit],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn('Restart=no', text); self.assertNotIn('[Install]', text)
            argv = n.command(self.package, manifest, role, state)
            self.assertIn('--web.listen-address=127.0.0.1:'+str(manifest['ports'][role]), argv)
            result = subprocess.run([*argv, '--help'], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn('--cluster.listen-address=', n.command(self.package, manifest, 'alertmanager', state))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        parent = n.ROOT/'build/serve-notify-process-tests'; parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent)); self.root.chmod(0o700)
        self.state = self.root/'state'; self.state.mkdir(mode=0o700)
        for role in n.ROLES: (self.state/role).mkdir(mode=0o700)
        n.pkg.save(self.state/'binding.json', dict(package=str(self.root), manifest_sha256='fixture'))
        self.output = self.root/'run'
        self.entry = self.root/'source/tools/serve_notify/child.py'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('import time\nwhile True: time.sleep(.1)\n')
        self.manifest = dict(repository=str(n.ROOT), python=dict(path='/usr/bin/python3'),
                             files={}, ports=dict(prometheus=19095))

    def invoke(self, observer):
        with mock.patch.object(n, 'verify', return_value=self.manifest), \
             mock.patch.object(n, 'observe', side_effect=observer):
            return n.run(self.root, 'fixture', 'prometheus', self.state, self.output)

    def test_normal_stop_reaps_actual_process(self):
        def observe(*_):
            (self.output/'stop.request.json').write_text('{}')
            return dict(owned=True)
        self.assertEqual(self.invoke(observe), 0)
        value = n.m.read(self.output/'terminal.json')
        self.assertEqual(value['state'], 'STOPPED')
        self.assertTrue(value['cleanup_confirmed'])
        self.assertEqual(value['starts'], 1)
        with n.claim(self.state, 'prometheus'): pass

    def test_unexpected_exit_is_failed_with_cleanup(self):
        self.entry.write_text('raise SystemExit(7)\n')
        self.assertEqual(self.invoke(lambda *_: dict(owned=True)), 1)
        value = n.m.read(self.output/'terminal.json')
        self.assertEqual(value['state'], 'FAILED')
        self.assertTrue(value['cleanup_confirmed'])
        self.assertEqual(value['child_returncode'], 7)
        self.assertEqual(value['restarts'], 0)

    def test_log_failure_still_cleans_child(self):
        self.entry.write_text('import time\nprint("output",flush=True)\nwhile True: time.sleep(.1)\n')
        with mock.patch.object(n.sup.RotatingLog, 'write', side_effect=OSError('fixture full')):
            self.assertEqual(self.invoke(lambda *_: dict(owned=True)), 1)
        value = n.m.read(self.output/'terminal.json')
        self.assertTrue(value['cleanup_confirmed'])
        self.assertIn('log capture failed', value['error'])

    def test_identity_failure_before_logger_still_reaps_child(self):
        original=n.sup.process_identity;children=[]
        def identity(pid):
            value=original(pid)
            if pid!=os.getpid():children.append(value);raise OSError('fixture early identity failure')
            return value
        with mock.patch.object(n.sup,'process_identity',side_effect=identity):
            self.assertEqual(self.invoke(lambda *_:{}),1)
        value=n.m.read(self.output/'terminal.json')
        self.assertEqual((value['state'],value['starts'],value['cleanup_confirmed']),('FAILED',1,True))
        self.assertIn('early identity failure',value['error']);self.assertTrue(children)
        self.assertTrue(all(not n.sup.processes.group_members(v['pid']) for v in children))

    def test_busy_state_refused_before_spawning(self):
        with n.claim(self.state, 'prometheus'), mock.patch.object(n.subprocess, 'Popen') as child:
            self.assertEqual(self.invoke(lambda *_: {}), 1)
            child.assert_not_called()
        value = n.m.read(self.output/'terminal.json')
        self.assertTrue(value['cleanup_confirmed']); self.assertEqual(value['starts'], 0)

    def test_absent_owner_not_reported_clean(self):
        self.output.mkdir(mode=0o700)
        n.pkg.save(self.output/'controller.json', dict(owner=dict(pid=2147483646,
            start_ticks=1, boot_id='00000000-0000-0000-0000-000000000000')))
        self.assertEqual(n.m.status(self.output)['state'], 'OWNER_ABSENT')
        self.assertFalse(n.m.stop(self.output)['cleanup_confirmed'])
        self.assertFalse((self.output/'terminal.json').exists())


if __name__ == '__main__': unittest.main()
