"""CPU packaging contracts; synthetic engine/assets, never starts a service."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, gateway_config, service
from tools.serve_recovery.test_gateway import config


class BundleTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/native-bundle-tests'))
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.directory = Path(self.temp.name)
        self.model = self.directory/'cpu-model'
        (self.model/'ple').mkdir(parents=True)
        (self.model/'weights.safetensors').write_bytes(b'cpu fixture only')
        (self.model/'ple/table.bin').write_bytes(b'cpu fixture only')
        self.binary, self.guardian, self.probe, self.ingress = [self.directory/name
            for name in ('engine', 'guardian', 'probe', 'ingress')]
        for path in (self.binary, self.guardian, self.probe, self.ingress):
            path.write_bytes(('cpu '+path.name).encode())
        self.policy = self.directory/'policy.json'
        self.backend = self.directory/'clients.json'
        value = config(self.directory)
        self.save(self.policy, value['policy'])
        self.save(self.backend, value['backend_policy'])
        self.build = self.directory/'native-build.json'
        self.save(self.build, dict(version=1,
            source_files={name: bundle.engine.sha(ROOT/name) for name in bundle.NATIVE_SOURCES},
            guardian_sha256=bundle.engine.sha(self.guardian),
            cuda_probe_sha256=bundle.engine.sha(self.probe),
            ingress_sha256=bundle.engine.sha(self.ingress)))
        self.base = dict(files={'q4t': bundle.engine.sha(self.binary)},
            protocol_sha256='c'*64, model_dir=str(self.model),
            python=dict(path=sys.executable, sha256='b'*64))
        self.patch = mock.patch.object(bundle.engine, 'verify', return_value=self.base)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.temp_cleanup = self.addCleanup(self.temp.cleanup)

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def prepare(self, **options):
        def engine_prepare(_binary, _release, _model, destination, **_kwargs):
            destination.mkdir(mode=0o700)
            shutil.copyfile(self.backend, destination/'client-policy.json')
            (destination/'client-policy.json').chmod(0o600)
            return dict(manifest_sha256='a'*64, binary_sha256=self.base['files']['q4t'])
        with mock.patch.object(bundle.engine, 'prepare', side_effect=engine_prepare):
            return bundle.prepare(self.binary, self.directory/'release.json', self.model,
                self.guardian, self.probe, Path('/nginx-is-not-installed'),
                self.policy, self.backend, self.directory/'package',
                native_receipt=self.build, native_ingress=self.ingress, **options)

    def test_default_package_has_native_binary_and_no_nginx_dependency(self):
        receipt = self.prepare()
        value = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['ingress_mode'], 'native')
        self.assertIn('bin/q4t-ingress', value['files'])
        self.assertNotIn('bin/nginx', value['files'])
        self.assertFalse((Path(receipt['package'])/'bin/nginx').exists())
        cfg = bundle.gateway_config_for(receipt['package'], value, self.directory)
        self.assertEqual(cfg['version'], 2)
        self.assertNotIn('nginx', cfg)

    def test_changed_native_binary_blocks_before_lease_acquisition(self):
        receipt = self.prepare()
        path = Path(receipt['package'])/'bin/q4t-ingress'
        path.chmod(0o700)
        path.write_bytes(b'changed')
        with mock.patch.object(service, 'acquire_lease') as lease, self.assertRaises(ValueError):
            service.launch(receipt['package'], receipt['manifest_sha256'], self.directory/'state')
        lease.assert_not_called()

    def test_native_build_receipt_must_bind_ingress_binary(self):
        value = json.loads(self.build.read_text())
        value['ingress_sha256'] = 'f'*64
        self.save(self.build, value)
        with self.assertRaisesRegex(ValueError, 'native build receipt'):
            self.prepare()
        self.assertFalse((self.directory/'package').exists())

    def test_native_build_receipt_cannot_omit_http_transport_source(self):
        value = json.loads(self.build.read_text())
        value['source_files'].pop('src/server/http_request.cpp')
        self.save(self.build, value)
        with self.assertRaisesRegex(ValueError, 'native build receipt'):
            self.prepare()

    def test_native_mode_does_not_silently_enable_tls_or_lan(self):
        with self.assertRaisesRegex(ValueError, 'TLS belongs'):
            self.prepare(certificate=self.policy, key=self.backend)
        value = json.loads(self.policy.read_text())
        value['listen_host'] = '192.168.1.2'
        self.save(self.policy, value)
        with self.assertRaisesRegex(ValueError, 'loopback'):
            self.prepare()

    def test_native_config_cannot_be_rendered_as_nginx(self):
        receipt = self.prepare()
        value = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        cfg = bundle.gateway_config_for(receipt['package'], value, self.directory)
        with self.assertRaisesRegex(ValueError, 'explicit nginx'):
            gateway_config.render(cfg, self.directory, 18184, 'e'*64)

    def test_cli_defaults_native_and_nginx_is_not_a_required_argument(self):
        arguments = ['prepare']
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                     'policy', 'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            arguments += ['--'+name, str(self.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(arguments), 0)
        self.assertEqual(prepare.call_args.kwargs['ingress_mode'], 'native')
        self.assertIsNone(prepare.call_args.args[5])


if __name__ == '__main__':
    unittest.main()
