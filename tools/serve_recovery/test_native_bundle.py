"""CPU packaging contracts; synthetic engine/assets, never starts a service."""
import contextlib
import fcntl
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
        self.assertEqual(value['version'], 3)
        self.assertIn('source/tools/serve_recovery/observation_io.py', value['files'])
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

    def test_v2_source_inventory_is_frozen_and_cannot_claim_isolated_io(self):
        self.assertNotIn('tools/serve_recovery/observation_io.py', bundle.V2_SOURCES)
        self.assertEqual(set(bundle.SOURCES)-set(bundle.V2_SOURCES),
                         {'tools/serve_recovery/observation_io.py'})

    def test_launcher_never_hands_guardian_a_regular_file_for_diagnostics(self):
        receipt = self.prepare()
        state = self.directory/'state'
        service.initialize(receipt['package'], receipt['manifest_sha256'], state)
        fd = os.open(self.directory/'cpu-lease', os.O_CREAT | os.O_RDWR, 0o600)
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        captured = {}
        def spawn(_command, **kwargs):
            diag = kwargs['stderr']
            self.assertEqual(kwargs['stdout'], diag)
            self.assertEqual(fcntl.fcntl(diag, fcntl.F_GET_SEALS), service.DIAGNOSTIC_SEALS)
            self.assertEqual(os.fstat(diag).st_size, service.DIAGNOSTIC_BYTES)
            os.write(diag, b'bounded startup reason')
            captured['diagnostic'] = os.pread(diag, 22, 0)
            return process
        with mock.patch.object(service, 'acquire_lease', return_value=fd), \
             mock.patch.object(service.subprocess, 'Popen', side_effect=spawn):
            result = service.launch(receipt['package'], receipt['manifest_sha256'], state)
        self.assertEqual(captured['diagnostic'], b'bounded startup reason')
        self.assertFalse((Path(result['run'])/'guardian.log').exists())

    def test_fatal_diagnostic_buffer_cannot_grow_and_is_readable_privately(self):
        fd = service.diagnostic_buffer()
        try:
            os.write(fd, b'x'*service.DIAGNOSTIC_BYTES)
            with self.assertRaises(OSError):
                os.write(fd, b'over capacity')
            self.assertEqual(os.fstat(fd).st_size, service.DIAGNOSTIC_BYTES)
            # A separate CPU child makes the real /proc/fd read use the same
            # identity checks as the private CLI, without changing test stderr.
            child = service.subprocess.Popen([sys.executable, '-B', '-c',
                'import time; time.sleep(20)'], stderr=fd)
            try:
                result = service.read_diagnostics(dict(guardian_pid=child.pid,
                    guardian_start_ticks=service.journal.process_start(child.pid)))
                self.assertTrue(result['possibly_truncated'])
                self.assertEqual(result['text'], 'x'*service.DIAGNOSTIC_BYTES)
                with self.assertRaisesRegex(ValueError, 'stale diagnostic owner'):
                    service.read_diagnostics(dict(guardian_pid=child.pid, guardian_start_ticks=0))
            finally:
                child.kill()
                child.wait(timeout=3)
        finally:
            os.close(fd)

    def test_optional_diagnostic_allocation_failure_still_launches(self):
        receipt = self.prepare()
        state = self.directory/'state'
        service.initialize(receipt['package'], receipt['manifest_sha256'], state)
        fd = os.open(self.directory/'cpu-lease', os.O_CREAT | os.O_RDWR, 0o600)
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(service, 'acquire_lease', return_value=fd), \
             mock.patch.object(service, 'diagnostic_buffer', side_effect=OSError('ENOMEM')), \
             mock.patch.object(service.subprocess, 'Popen', return_value=process) as spawn:
            service.launch(receipt['package'], receipt['manifest_sha256'], state)
        self.assertEqual(spawn.call_args.kwargs['stderr'], service.subprocess.DEVNULL)

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
