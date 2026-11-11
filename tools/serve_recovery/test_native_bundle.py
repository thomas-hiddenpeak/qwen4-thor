"""CPU packaging contracts; synthetic engine/assets, never starts a service."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
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
            settings=bundle.engine.settings(18182, 0, operations_port=18183),
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
            bundle.engine.save(destination/'manifest.json', self.base)
            return dict(manifest_sha256='a'*64, binary_sha256=self.base['files']['q4t'])
        with mock.patch.object(bundle.engine, 'prepare', side_effect=engine_prepare):
            return bundle.prepare(self.binary, self.directory/'release.json', self.model,
                self.guardian, self.probe, Path('/nginx-is-not-installed'),
                self.policy, self.backend, self.directory/'package',
                native_receipt=self.build, native_ingress=self.ingress, **options)

    def test_default_package_has_native_binary_and_no_nginx_dependency(self):
        receipt = self.prepare()
        value = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['version'], 18)
        self.assertNotIn('bin/q4t-device-control', value['files'])
        self.assertIn('source/tools/serve_recovery/cold_admission.py', value['files'])
        self.assertIn('source/tools/serve_recovery/quota_manager.py', value['files'])
        self.assertIn('source/tools/serve_recovery/quota_runtime.py', value['files'])
        self.assertIn('source/tools/serve_recovery/usage_runtime.py', value['files'])
        self.assertIn('source/tools/serve_recovery/usage_io.py', value['files'])
        self.assertIn('source/tools/serve_recovery/access_manager.py', value['files'])
        self.assertIn('source/tools/serve_recovery/access_io.py', value['files'])
        self.assertIn('source/tools/serve_recovery/observation_io.py', value['files'])
        self.assertIn('source/tools/serve_recovery/progress.py', value['files'])
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

    def test_dynamic_profile_keeps_recovery_gate_when_initial_quotas_are_off(self):
        value = json.loads(self.backend.read_bytes())
        for client in value['clients']:
            client['requests_per_minute'] = client['tokens_per_minute'] = 0
        self.save(self.backend, value)
        receipt = self.prepare();manifest = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertTrue(manifest['quota_enforced'])
        cfg = bundle.gateway_config_for(receipt['package'], manifest, self.directory)
        self.assertEqual(cfg['backend_policy']['version'], 3)
        self.assertFalse(bundle.binding.quota_enforced(cfg['backend_policy']))

    def test_v2_source_inventory_is_frozen_and_cannot_claim_isolated_io(self):
        self.assertEqual(bundle.V9_SOURCES, bundle.V8_SOURCES)
        self.assertEqual(bundle.V11_SOURCES, bundle.V10_SOURCES)
        self.assertEqual(set(bundle.SOURCES)-set(bundle.V17_SOURCES),
                         {'tools/serve_recovery/'+name+'.py' for name in
                          ('outer_policy', 'outer_store', 'outer_instance', 'outer_tree',
                           'outer_reconcile', 'outer_observe', 'outer_manager', 'outer_control', 'outer_runtime', 'resource_kernel', 'resource_install', 'resource_launch', 'resource_reconcile')})
        self.assertEqual(set(bundle.V17_SOURCES)-set(bundle.V16_SOURCES),
            {'tools/serve_recovery/'+name+'.py' for name in ('cold_admission',
             'device_budget', 'device_store', 'device_control', 'device_inventory',
             'device_platform', 'device_domain', 'cold_compat')})
        self.assertEqual(set(bundle.V16_SOURCES)-set(bundle.V15_SOURCES),
            {'tools/serve_recovery/'+name+'.py' for name in ('version_runtime','version_launch')})
        self.assertEqual(set(bundle.V15_SOURCES)-set(bundle.V14_SOURCES),
            {'tools/serve_recovery/'+name+'.py' for name in ('version_policy',
             'version_store', 'version_journal', 'version_contract', 'version_registration')})
        self.assertEqual(set(bundle.V14_SOURCES)-set(bundle.V13_SOURCES),
                         {'tools/serve_recovery/profile_compat.py'})
        self.assertEqual(set(bundle.V13_SOURCES)-set(bundle.V12_SOURCES),
                         {'tools/serve_recovery/profile_migration.py'})
        self.assertEqual(set(bundle.V12_SOURCES)-set(bundle.V11_SOURCES),
            {'tools/serve_recovery/'+name+'.py' for name in
             ('profile_contract', 'profile_policy', 'profile_memory', 'profile_store',
              'profile_io', 'profile_runtime', 'profile_controller', 'profile_evidence',
              'profile_bundle', 'profile_manager')})
        self.assertEqual(set(bundle.V10_SOURCES)-set(bundle.V9_SOURCES),
                         {'tools/serve_service/capacity.py', 'tools/serve_recovery/lifecycle.py',
                          *('tools/serve_recovery/'+n+'.py' for n in
                            ('publication', 'publication_policy', 'publication_files', 'publication_journal'))})
        self.assertNotIn('tools/serve_recovery/observation_io.py', bundle.V2_SOURCES)
        self.assertEqual(set(bundle.V3_SOURCES)-set(bundle.V2_SOURCES),
                         {'tools/serve_recovery/observation_io.py'})
        self.assertEqual(set(bundle.V4_SOURCES)-set(bundle.V3_SOURCES),
                         {'tools/serve_recovery/tls_control.py'})
        self.assertEqual(set(bundle.V5_SOURCES)-set(bundle.V4_SOURCES),
                         {'tools/serve_recovery/progress.py'})
        self.assertEqual(set(bundle.V6_SOURCES)-set(bundle.V5_SOURCES),
                         {'tools/serve_host/native.py', 'tools/serve_host/native_exporter.py',
                          'tools/serve_recovery/retention.py', 'tools/serve_recovery/retention_epoch.py'})
        self.assertEqual(set(bundle.V7_SOURCES)-set(bundle.V6_SOURCES),
            {'tools/serve_recovery/'+n+'.py' for n in ('access_policy', 'access_store',
             'access_control', 'access_io', 'access_manager')})
        self.assertEqual(set(bundle.V8_SOURCES)-set(bundle.V7_SOURCES),
            {'tools/serve_recovery/'+n+'.py' for n in ('quota_policy', 'quota_store',
             'quota_control', 'quota_runtime', 'quota_manager', 'usage_policy',
             'usage_store', 'usage_io', 'usage_control', 'usage_runtime',
             'model_admin', 'usage_manager', 'deployment_policy', 'deployment', 'initialization')})

    def test_historical_source_inventory_versions_remain_verifiable(self):
        receipt = self.prepare()
        package = Path(receipt['package'])
        current = bundle.engine.read(package/'manifest.json')
        for version, sources in ((1, bundle.LEGACY_SOURCES),
                (2, bundle.V2_SOURCES), (3, bundle.V3_SOURCES),
                (4, bundle.V4_SOURCES), (5, bundle.V5_SOURCES), (6, bundle.V6_SOURCES),
                (7, bundle.V7_SOURCES), (8, bundle.V8_SOURCES), (9, bundle.V9_SOURCES),
                (10, bundle.V10_SOURCES), (11, bundle.V11_SOURCES),
                (12, bundle.V12_SOURCES), (13, bundle.V13_SOURCES), (14, bundle.V14_SOURCES),
                (15, bundle.V15_SOURCES), (16, bundle.V16_SOURCES),
                (17, bundle.V17_SOURCES)):
            with self.subTest(version=version):
                value = dict(current, version=version,
                    profile='q4t-service-recovery-v'+str(version))
                if version < 12:
                    value.pop('recovery_profiles');value.pop('recovery_profile_policy')
                names = {*('source/'+name for name in sources),
                    'gateway-policy.json', 'native-build.json',
                    *('bin/'+name for name in bundle.EXECUTABLES)}
                if version < 4:
                    value.pop('tls_configuration')
                if version == 1:
                    value.pop('ingress_mode')
                    shutil.copyfile(package/'bin/q4t-ingress', package/'bin/nginx')
                    names.remove('bin/q4t-ingress')
                    names.add('bin/nginx')
                value['files'] = {name: bundle.engine.sha(package/name) for name in names}
                bundle.engine.save(package/'manifest.json', value)
                digest = bundle.engine.sha(package/'manifest.json')
                (package/'bin/q4t').chmod(0o700)
                (package/'bin/q4t').write_bytes(bundle.launcher(digest,
                    value['python']['path']))
                self.assertEqual(bundle.verify(package, digest)['version'], version)

    def http_policy(self):
        value = json.loads(self.policy.read_bytes())
        value.update(version=4, cors=None, http=dict(path_prefix='/llm/thor',
            keep_alive_requests=7, keep_alive_idle_seconds=3,
            request_timeout_seconds=12, proxy=dict(trusted_cidrs=['127.0.0.1/32'],
                header='forwarded', allow_stripped_prefix=True)))
        self.save(self.policy, value)
        return bundle.ingress.policy(value)

    def test_file_transport_and_client_capacity_require_v11_identity(self):
        public = self.http_policy()
        public.update(version=5, file_body_bytes=512*1024**2+65536)
        self.save(self.policy, public)
        backend = bundle.ingress.parse(self.backend.read_bytes())
        backend.update(version=4, revision=0)
        for client in backend['clients']:
            client['max_file_body_bytes'] = public['file_body_bytes']
        self.save(self.backend, backend)
        receipt = self.prepare()
        package = Path(receipt['package'])
        value = bundle.verify(package, receipt['manifest_sha256'])
        self.assertEqual(value['version'], 18)
        self.assertIn('include/q4t/server/file_limits.h', bundle.NATIVE_SOURCES)
        # Re-signing a v10 label cannot grant capabilities its format lacks.
        value.update(version=10, profile='q4t-service-recovery-v10')
        value.pop('recovery_profiles');value.pop('recovery_profile_policy')
        historical_sources = {'source/'+name for name in bundle.V10_SOURCES}
        value['files'] = {n:h for n,h in value['files'].items()
                          if not n.startswith('source/') or n in historical_sources}
        def sign():
            bundle.engine.save(package/'manifest.json', value)
            digest = bundle.engine.sha(package/'manifest.json')
            (package/'bin/q4t').chmod(0o700)
            (package/'bin/q4t').write_bytes(bundle.launcher(digest, value['python']['path']))
            return digest
        with self.assertRaisesRegex(ValueError, 'file HTTP policy'):
            bundle.verify(package, sign())
        public.pop('file_body_bytes');public['version'] = 4
        self.save(package/'gateway-policy.json', public)
        value['files']['gateway-policy.json'] = bundle.engine.sha(package/'gateway-policy.json')
        with self.assertRaisesRegex(ValueError, 'file client policy'):
            bundle.verify(package, sign())

    def test_http_profile_seals_normalized_policy_and_preserves_managed_binding(self):
        policy = self.http_policy()
        receipt = self.prepare()
        value = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['version'], 18)
        self.assertEqual(bundle.engine.read(Path(receipt['package'])/'gateway-policy.json'), policy)
        cfg = bundle.gateway_config_for(receipt['package'], value, self.directory,
            access=dict(state_directory=str(self.directory/'state'), state_identity=[1, 2],
                        service_id='11111111-1111-4111-8111-111111111111'))
        self.assertEqual(cfg['version'], 4)
        self.assertEqual(cfg['policy'], policy)
        self.assertEqual(cfg['backend_policy']['version'], 3)
        self.assertTrue(value['quota_enforced'])

    def test_http_profile_without_optional_proxy_normalizes_safe_defaults(self):
        value = self.http_policy()
        value['http'] = {}
        self.save(self.policy, value)
        receipt = self.prepare()
        manifest = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        actual = bundle.gateway_config_for(receipt['package'], manifest, self.directory)
        self.assertEqual(actual['policy']['http'], bundle.ingress.http_policy({}))
        self.assertIsNone(actual['policy']['http']['proxy'])

    def test_http_policy_cannot_claim_an_old_transport_profile(self):
        self.http_policy()
        receipt = self.prepare()
        package = Path(receipt['package'])
        value = bundle.engine.read(package/'manifest.json')
        value.update(version=8, profile='q4t-service-recovery-v8')
        bundle.engine.save(package/'manifest.json', value)
        digest = bundle.engine.sha(package/'manifest.json')
        (package/'bin/q4t').chmod(0o700)
        (package/'bin/q4t').write_bytes(bundle.launcher(digest, value['python']['path']))
        with self.assertRaisesRegex(ValueError, 'transport profile v9'):
            bundle.verify(package, digest)

    def test_http_profile_refuses_nginx_mode_before_creating_package(self):
        self.http_policy()
        with self.assertRaisesRegex(ValueError, 'legacy policy'):
            self.prepare(ingress_mode='nginx')
        self.assertFalse((self.directory/'package').exists())

    def test_transport_build_receipt_requires_framing_and_proxy_sources(self):
        original = json.loads(self.build.read_bytes())
        for name in ('include/q4t/server/http_framing.h',
                     'include/q4t/service/ingress_proxy.h', 'src/service/ingress_proxy.cpp'):
            with self.subTest(source=name):
                value = json.loads(json.dumps(original))
                value['source_files'].pop(name)
                self.save(self.build, value)
                with self.assertRaisesRegex(ValueError, 'native build receipt'):
                    self.prepare()
                self.assertFalse((self.directory/'package').exists())

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
        guardian = json.loads((Path(result['run'])/'guardian.json').read_bytes())
        self.assertEqual(guardian['version'], 2)
        self.assertTrue(guardian['usage_handoff'])
        self.assertTrue((state/'client-usage.json').is_file())

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

    def test_failed_startup_socket_closes_lease_copy_without_unlocking_owner(self):
        receipt = self.prepare()
        state = self.directory/'state'
        service.initialize(receipt['package'], receipt['manifest_sha256'], state)
        path = self.directory/'cpu-only-lock'
        with path.open('w+') as owner, path.open('r+') as competitor:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            borrowed = os.dup(owner.fileno())
            with mock.patch.object(service, 'acquire_lease', return_value=borrowed), \
                 mock.patch.object(service.socket, 'socketpair', side_effect=OSError('EMFILE')), \
                 mock.patch.object(service.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(OSError, 'EMFILE'):
                    service.launch(receipt['package'], receipt['manifest_sha256'], state,
                                   daemon=True)
            spawn.assert_not_called()
            with self.assertRaises(OSError):
                os.fstat(borrowed)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner.close()
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

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

    def test_optional_native_device_control_is_source_bound_and_hash_checked(self):
        native = self.directory/'device-control'
        native.write_bytes(b'CPU device control binary fixture')
        build = json.loads(self.build.read_bytes())
        build['device_control_sha256'] = bundle.engine.sha(native)
        self.save(self.build, build)
        receipt = self.prepare(device_control=native)
        package = Path(receipt['package'])
        value = bundle.verify(package, receipt['manifest_sha256'])
        self.assertEqual(value['files']['bin/q4t-device-control'], build['device_control_sha256'])
        self.assertIn('src/service/device_control_main.cpp', build['source_files'])
        self.assertIn('src/service/device_admission.cpp', build['source_files'])
        self.assertIn('src/service/device_filter.cpp', build['source_files'])
        copied = package/'bin/q4t-device-control'
        copied.chmod(0o700); copied.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'file identity changed'):
            bundle.verify(package, receipt['manifest_sha256'])

    def test_device_control_receipt_and_custody_source_cannot_be_omitted(self):
        native = self.directory/'device-control'; native.write_bytes(b'CPU fixture')
        original = json.loads(self.build.read_bytes())
        with self.assertRaisesRegex(ValueError, 'native build receipt'):
            self.prepare(device_control=native)
        original['device_control_sha256'] = bundle.engine.sha(native)
        original['source_files'].pop('src/service/device_filter.cpp')
        self.save(self.build, original)
        with self.assertRaisesRegex(ValueError, 'native build receipt'):
            self.prepare(device_control=native)
        self.assertFalse((self.directory/'package').exists())

    def test_relocated_v18_source_imports_every_cold_management_module(self):
        receipt = self.prepare()
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from tools.serve_recovery import service, cold_admission, device_budget
from tools.serve_recovery import device_store, device_control, device_inventory
from tools.serve_recovery import device_platform, device_domain
assert cold_admission.configuration('unused', 'unused', -1) == (None, ())
print('isolated cold source imports passed')
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(Path(receipt['package'])/'source')], cwd=self.directory,
            env={}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b'isolated cold source imports passed\n')

    def test_relocated_v18_contains_complete_outer_and_resume_sources(self):
        receipt = self.prepare()
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from tools.serve_recovery import service, lifecycle, outer_policy, outer_store
from tools.serve_recovery import outer_instance, outer_tree, outer_reconcile
from tools.serve_recovery import outer_observe, outer_manager, outer_control, outer_runtime
from tools.serve_recovery import resource_kernel, resource_install, resource_launch, resource_reconcile
assert not lifecycle.pending_outer(sys.argv[2], {})
service.main(['resume', '--help'])
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(Path(receipt['package'])/'source'), str(self.directory)], cwd=self.directory,
            env={}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b'resume', result.stdout)

    def test_application_limits_must_be_bound_before_package_creation(self):
        original = json.loads(self.build.read_text())
        name = 'include/q4t/server/request_limits.h'
        self.assertIn(name, original['source_files'])
        for missing in (True, False):
            with self.subTest(missing=missing):
                value = json.loads(json.dumps(original))
                if missing:
                    value['source_files'].pop(name)
                else:
                    value['source_files'][name] = '0'*64
                self.save(self.build, value)
                with self.assertRaisesRegex(ValueError, 'native (build receipt|source changed)'):
                    self.prepare()
                self.assertFalse((self.directory/'package').exists())

    def test_native_mode_validates_tls_before_packaging_and_rejects_plaintext_lan(self):
        with mock.patch.object(bundle.tls_control, 'check',
                side_effect=ValueError('native TLS credential validation failed')), \
             self.assertRaisesRegex(ValueError, 'TLS credential'):
            self.prepare(certificate=self.policy, key=self.backend)
        value = json.loads(self.policy.read_text())
        value['listen_host'] = '192.168.1.2'
        self.save(self.policy, value)
        with self.assertRaisesRegex(ValueError, 'loopback'):
            self.prepare()

    def test_native_tls_paths_are_sealed_but_credentials_are_reloadable(self):
        with mock.patch.object(bundle.tls_control, 'check', return_value={'ready': True}) as check:
            receipt = self.prepare(certificate=self.policy, key=self.backend)
        check.assert_called_once()
        value = bundle.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['tls_configuration']['certificate'], str(self.policy))
        self.assertNotIn('private-key.pem', value['files'])
        cfg = bundle.gateway_config_for(receipt['package'], value, self.directory)
        self.assertTrue(value['tls'])
        self.assertEqual(cfg['tls'], value['tls_configuration'])

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
        self.assertIsNone(prepare.call_args.kwargs['device_control'])
        path = self.directory/'q4t-device-control'
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main([*arguments, '--device-control', str(path)]), 0)
        self.assertEqual(prepare.call_args.kwargs['device_control'], path)

    def test_cli_carries_application_state_limits_to_sealed_package(self):
        arguments = ['prepare']
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                     'policy', 'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            arguments += ['--'+name, str(self.directory/name)]
        arguments += ['--object-store-dir', '/persistent/objects',
                      '--object-max-client-mib', '128', '--object-io-timeout-ms', '500']
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(arguments), 0)
        self.assertEqual(prepare.call_args.kwargs['application_state'],
                         dict(directory='/persistent/objects', max_client_mib=128,
                              io_timeout_ms=500))

    def test_cli_carries_capacity_input_to_sealed_package(self):
        path = self.directory/'capacity.json'
        selected = dict(version=1, max_len=4096, mtp_enabled=False)
        self.save(path, selected)
        arguments = ['prepare', '--capacity-config', str(path)]
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                     'policy', 'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            arguments += ['--'+name, str(self.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(arguments), 0)
        self.assertEqual(prepare.call_args.kwargs['capacity_config'], selected)

    def test_capacity_overcommit_rejected_before_outer_package_creation(self):
        with self.assertRaisesRegex(RuntimeError, 'client fairness'):
            self.prepare(capacity_config=dict(version=1, max_inflight_requests=3))
        self.assertFalse((self.directory/'package').exists())


if __name__ == '__main__':
    unittest.main()
