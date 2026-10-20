"""CPU substitutes only: tiny fake binaries, no real subprocess/GPU/cache work."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location('delivery_deploy_tested', HERE / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
REAL_OLD_SHA = deploy.OLD_SHA


class DeployTest(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.q4t-work/startup-delivery-20261021/host-deploy-scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / '.q4t-work/startup-delivery-20261021'
        self.base.mkdir(parents=True)
        self.output = self.base / 'deploy-v1'
        self.package = self.root / '.q4t-work/releases/new'
        self.decision = self.root / 'decision.md'
        self.decision.write_text('formal delivery authorization fixture\n')
        self.old_sha = hashlib.sha256(b'old fake executable').hexdigest()
        self.patches = [
            patch.object(deploy, 'ROOT', self.root),
            patch.object(deploy, 'BASE', self.base),
            patch.object(deploy, 'DECISION', self.decision),
            patch.object(deploy, 'OLD_SHA', self.old_sha),
            patch.object(deploy.build, 'verify', return_value={'status': 'PASS'}),
            patch.object(deploy.subprocess, 'Popen',
                         side_effect=AssertionError('real process forbidden')),
            patch.object(deploy.subprocess, 'run',
                         side_effect=AssertionError('real process forbidden')),
            patch.object(deploy.subprocess, 'check_output',
                         side_effect=AssertionError('real process forbidden')),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.lock = self.root / '.q4t-work/gpu-exclusive.lock'
        fake_runner = SimpleNamespace(
            common_lock=lambda: self.lock,
            helper_cleanup_evidence=lambda _out: dict(started=False),
            scope_properties=lambda _unit: dict(LoadState='not-found'))
        p = patch.object(deploy, '_runner', fake_runner)
        p.start()
        self.addCleanup(p.stop)
        binary, sidecar = deploy.destinations()
        binary.parent.mkdir()
        binary.write_bytes(b'old fake executable')
        binary.chmod(0o755)
        old_cache = self.root / '.q4t-work/releases/old/CMakeCache.txt'
        old_cache.parent.mkdir(parents=True)
        old_cache.write_text('old true cache\n')
        deploy.save(sidecar, dict(binary_sha256=self.old_sha,
            build_cache=str(old_cache), build_cache_sha256=deploy.sha(old_cache)))
        self.old_pair = dict(binary=deploy.identity(binary),
                            sidecar=deploy.identity(sidecar),
                            build_cache=deploy.identity(old_cache))
        candidate = self.base / 'candidate-v1/build/q4t'
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b'new arbitrary fake executable, not hardcoded')
        candidate.chmod(0o755)
        cache = candidate.with_name('CMakeCache.txt')
        cache.write_text('real new build cache\n')
        source = self.base / 'candidate-v1/source-inputs.json'
        deploy.save(source, dict(actual='frozen compiler inputs'))
        self.manifest_path = self.base / 'candidate-v1/build.json'
        self.manifest = dict(status='COMPLETED', candidate=deploy.identity(candidate),
            candidate_sha256=deploy.sha(candidate), default_sha256=self.old_sha,
            default_pair=self.old_pair, real_build_cache=deploy.identity(cache),
            source_manifest=deploy.identity(source))
        deploy.save(self.manifest_path, self.manifest)
        deploy.save(self.manifest_path.parent / 'attempt.json', dict(
            source_commit='3f2c704848088228494eaae99de994f9012d6321',
            default_pair=self.old_pair))
        inputs = {}
        for stage in deploy.STAGES:
            out = self.base / stage
            out.mkdir()
            (out / 'proof.txt').write_text(stage + '\n')
            identity = deploy.seal(out)
            inputs[identity['path']] = identity
        failed = self.base / 'boundary-v1'
        failed.mkdir()
        (failed / 'proof.txt').write_text('cold gate failed before model/HTTP\n')
        old_seal = deploy.seal(failed)
        old_audit = self.base / 'audit-boundary-v1.json'
        deploy.save(old_audit, dict(status='INCOMPLETE', runtime_accepted=False,
            errors=[], candidate_sha256=self.manifest['candidate_sha256'],
            stages={'boundary-v1': dict(status='RUNTIME_NOT_ACCEPTED',
                runtime_accepted=False, model_starts=0, http_requests=0,
                preparations=1)}))
        old_audit = deploy.identity(old_audit)
        inputs[old_seal['path']] = old_seal
        inputs[old_audit['path']] = old_audit
        retained = dict(name='boundary-v1', status='PREPARATION_FAILED',
            runtime_accepted=False, model_starts=0, http_requests=0,
            preparations=1, candidate_sha256=self.manifest['candidate_sha256'],
            sealed_manifest=old_seal, audit=old_audit)
        self.audit = dict(schema=1, status='PASS_BOUNDED_RELIABILITY',
            runtime_accepted=True, errors=[], candidate=self.manifest['candidate'],
            build_manifest=deploy.identity(self.manifest_path),
            source_manifest=self.manifest['source_manifest'], default=self.old_pair,
            stages={name: dict(status='PASS') for name in deploy.STAGES},
            budget=dict(model_starts=6, http_requests=9, preparations=7),
            retained_failures=[retained],
            evidence_inputs=inputs)
        self.audit_path = self.base / 'audit-final-v1.json'
        deploy.save(self.audit_path, self.audit)

    def preflight(self):
        return deploy.preflight(self.manifest_path, self.audit_path,
                                self.output, self.package)

    def prepare(self):
        context = self.preflight()
        self.output.mkdir()
        deploy.save(self.output / 'context.json', context)
        return deploy.prepare_package(context)

    def assert_old(self):
        binary, sidecar = deploy.destinations()
        self.assertEqual(deploy.identity(binary), self.old_pair['binary'])
        self.assertEqual(deploy.identity(sidecar), self.old_pair['sidecar'])

    def rewrite_audit(self, value):
        self.audit_path.write_text(json.dumps(value))

    def test_old_identity_constant_and_read_only_preflight(self):
        self.assertEqual(REAL_OLD_SHA,
            '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469')
        result = self.preflight()
        self.assertEqual(result['candidate'], self.manifest['candidate'])
        self.assertEqual((result['deployment_model_limit'],
                          result['deployment_http_limit']), (2, 2))
        deploy.build.verify.assert_called_once_with(self.manifest_path,
            check_workspace=True, check_default=True)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.package.exists())
        self.assert_old()

    def test_partial_or_unaccepted_audit_rejected(self):
        for key, value in [('status', 'NOT_RUN'), ('runtime_accepted', False),
                           ('errors', ['quality failed'])]:
            with self.subTest(key=key):
                altered = dict(self.audit, **{key: value})
                self.rewrite_audit(altered)
                with self.assertRaises(RuntimeError):
                    self.preflight()
        altered = copy.deepcopy(self.audit)
        altered['stages'].pop(deploy.STAGES[-1])
        self.rewrite_audit(altered)
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assert_old()

    def test_audit_budget_and_candidate_binding_rejected(self):
        altered = copy.deepcopy(self.audit)
        altered['budget']['http_requests'] = 8
        self.rewrite_audit(altered)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            self.preflight()
        altered = copy.deepcopy(self.audit)
        altered['candidate']['sha256'] = '0' * 64
        self.rewrite_audit(altered)
        with self.assertRaisesRegex(RuntimeError, 'binding'):
            self.preflight()

    def test_revised_stage_and_preparation_count_required(self):
        self.assertIn('boundary-v2', deploy.STAGES)
        self.assertNotIn('boundary-v1', deploy.STAGES)
        self.assertEqual(self.preflight()['candidate_budget']['preparations'], 7)
        altered = copy.deepcopy(self.audit)
        altered['budget']['preparations'] = 6
        self.rewrite_audit(altered)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            self.preflight()
        altered = copy.deepcopy(self.audit)
        altered['stages']['boundary-v1'] = altered['stages'].pop('boundary-v2')
        self.rewrite_audit(altered)
        with self.assertRaisesRegex(RuntimeError, 'all six'):
            self.preflight()

    def test_retained_failure_cannot_be_omitted_or_promoted(self):
        for replacement in (None, [], [self.audit['retained_failures'][0]] * 2):
            with self.subTest(replacement=replacement):
                altered = copy.deepcopy(self.audit)
                altered['retained_failures'] = replacement
                self.rewrite_audit(altered)
                with self.assertRaisesRegex(RuntimeError, 'exactly one retained'):
                    self.preflight()
        for key, value in (('name', 'other-v1'), ('status', 'PASS'),
                           ('runtime_accepted', True), ('model_starts', 1),
                           ('http_requests', 1), ('preparations', 0),
                           ('candidate_sha256', '0' * 64)):
            with self.subTest(key=key):
                altered = copy.deepcopy(self.audit)
                altered['retained_failures'][0][key] = value
                self.rewrite_audit(altered)
                with self.assertRaisesRegex(RuntimeError, 'retained failure'):
                    self.preflight()

    def test_retained_failure_seal_and_independent_audit_required(self):
        failed = self.audit['retained_failures'][0]
        for record in (failed['sealed_manifest'], failed['audit']):
            with self.subTest(binding=record['path']):
                altered = copy.deepcopy(self.audit)
                altered['evidence_inputs'].pop(record['path'])
                self.rewrite_audit(altered)
                with self.assertRaisesRegex(RuntimeError, 'not bound'):
                    self.preflight()
        self.rewrite_audit(self.audit)
        proof = self.base / 'boundary-v1/proof.txt'
        proof.write_text('changed retained failure')
        with self.assertRaisesRegex(RuntimeError, 'sealed file differs'):
            self.preflight()

    def test_changed_source_cache_or_binary_rejected(self):
        for key in ('source_manifest', 'real_build_cache', 'candidate'):
            with self.subTest(key=key):
                path = Path(self.manifest[key]['path'])
                original = path.read_bytes()
                path.write_bytes(b'unknown data')
                if key == 'real_build_cache':
                    deploy.build.verify.side_effect = RuntimeError('build cache differs')
                with self.assertRaises(RuntimeError):
                    self.preflight()
                deploy.build.verify.side_effect = None
                path.write_bytes(original)
        self.assert_old()

    def test_raw_stage_tampering_and_missing_binding_rejected(self):
        proof = self.base / deploy.STAGES[0] / 'proof.txt'
        original = proof.read_text()
        proof.write_text('modified evidence')
        with self.assertRaisesRegex(RuntimeError, 'sealed file differs'):
            self.preflight()
        proof.write_text(original)
        altered = copy.deepcopy(self.audit)
        altered['evidence_inputs'].pop(next(iter(altered['evidence_inputs'])))
        self.rewrite_audit(altered)
        with self.assertRaisesRegex(RuntimeError, 'not bound'):
            self.preflight()

    def test_previous_and_release_packages_complete_before_replace(self):
        plan = self.prepare()
        self.assert_old()
        deploy.verify_seal(self.output / 'previous', 'manifest.json')
        deploy.verify_seal(self.package, 'manifest.json')
        metadata = deploy.read(self.package / 'q4t.release.json')
        self.assertEqual(metadata['source_commit'],
                         '3f2c704848088228494eaae99de994f9012d6321')
        self.assertEqual(metadata['binary_sha256'], plan['candidate_sha256'])
        self.assertIn('not a cross-file', metadata['atomicity'])
        self.assertEqual(deploy.sha(self.package / 'CMakeCache.txt'),
                         self.manifest['real_build_cache']['sha256'])
        self.assertFalse((self.output / 'installation-started.json').exists())

    def test_install_and_independent_rollback(self):
        self.prepare()
        deploy.install(self.output)
        deploy.verify_published_pair(self.output)
        # Recovery does not require live source, candidate build, or audit.
        Path(self.manifest['candidate']['path']).unlink()
        self.audit_path.unlink()
        self.assertEqual(deploy.rollback(self.output)['status'], 'ROLLED_BACK')
        self.assert_old()

    def test_all_four_known_pairs_can_rollback(self):
        plan = self.prepare()
        binary, sidecar = deploy.destinations()
        previous = self.output / 'previous'
        for binary_new in (False, True):
            for release_new in (False, True):
                with self.subTest(binary_new=binary_new, release_new=release_new):
                    binary.write_bytes((self.package if binary_new else previous)
                                       .joinpath('q4t').read_bytes())
                    sidecar.write_bytes((self.package if release_new else previous)
                                        .joinpath('q4t.release.json').read_bytes())
                    self.assertEqual(deploy.rollback(self.output)['status'],
                                     'ROLLED_BACK')
                    self.assert_old()
        self.assertNotEqual(plan['candidate_sha256'], self.old_sha)

    def test_unknown_half_refuses_any_rollback_write(self):
        self.prepare()
        deploy.install(self.output)
        binary, sidecar = deploy.destinations()
        for unknown in (binary, sidecar):
            with self.subTest(unknown=unknown.name):
                saved = unknown.read_bytes()
                unknown.write_bytes(b'unknown external identity')
                before = [p.read_bytes() for p in (binary, sidecar)]
                with self.assertRaisesRegex(RuntimeError, 'unknown current pair'):
                    deploy.rollback(self.output)
                self.assertEqual(before, [p.read_bytes() for p in (binary, sidecar)])
                unknown.write_bytes(saved)

    def test_second_replace_failure_keeps_recoverable_partial_pair(self):
        self.prepare()
        original = deploy.replace_checked
        calls = []
        def fail_sidecar(source, target, expected):
            calls.append(target)
            if target.name == 'q4t.release.json':
                raise OSError('simulated second rename failure')
            return original(source, target, expected)
        with patch.object(deploy, 'replace_checked', side_effect=fail_sidecar):
            with self.assertRaises(OSError):
                deploy.install(self.output)
        binary, sidecar = deploy.destinations()
        self.assertEqual(deploy.sha(binary), self.manifest['candidate_sha256'])
        self.assertEqual(deploy.sha(sidecar), self.old_pair['sidecar']['sha256'])
        deploy.rollback(self.output)
        self.assert_old()

    def test_unknown_current_refused_before_install(self):
        self.prepare()
        binary, sidecar = deploy.destinations()
        sidecar.write_text('new external metadata')
        with self.assertRaisesRegex(RuntimeError, 'default changed'):
            deploy.install(self.output)
        self.assertEqual(deploy.sha(binary), self.old_sha)
        self.assertEqual(sidecar.read_text(), 'new external metadata')

    def test_corrupt_backup_cannot_overwrite_default(self):
        self.prepare()
        deploy.install(self.output)
        (self.output / 'previous/q4t').write_text('corrupt rollback binary')
        before = [p.read_bytes() for p in deploy.destinations()]
        with self.assertRaisesRegex(RuntimeError, 'sealed file differs'):
            deploy.rollback(self.output)
        self.assertEqual(before, [p.read_bytes() for p in deploy.destinations()])

    def test_missing_old_cache_restored_from_verified_backup(self):
        self.prepare()
        deploy.install(self.output)
        old_cache = Path(self.old_pair['build_cache']['path'])
        old_cache.unlink()
        deploy.rollback(self.output)
        self.assertEqual(deploy.identity(old_cache), self.old_pair['build_cache'])
        self.assertNotEqual(old_cache.stat().st_ino,
            (self.output / 'previous/CMakeCache.txt').stat().st_ino,
            'restored cache aliases immutable rollback backup')

    def test_budget_marker_rejects_implicit_second_deployment(self):
        deploy.save(self.base / 'deployment-attempt.json', dict(started=True))
        with self.assertRaisesRegex(RuntimeError, 'no additional model budget'):
            self.preflight()

    def test_unheld_lease_rejects_internal_worker(self):
        deploy.save(self.lock, dict(state='held', output=str(self.output / 'lease')))
        with self.assertRaisesRegex(RuntimeError, 'requires the GPU lease'):
            deploy.require_guard(self.output)

    def run_fake_manager(self, *, failed_health=False, guard_cleanup_error=False):
        calls = []
        def health(output, stage):
            calls.append(stage)
            if failed_health and stage == deploy.HEALTH[1]:
                raise RuntimeError('synthetic exact-output failure')
            out = output / stage
            out.mkdir()
            deploy.save(out / 'health.json', dict(status='PASS'))
            return dict(stage=stage, health=dict(status='PASS'),
                        sealed_files=deploy.seal(out))
        outer = self
        class GuardProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                outer.assertIn('--lease-fd', command)
                outer.assertEqual(command[command.index('--compute-backend') + 1],
                                  'nvml')
                outer.assertTrue(kwargs['pass_fds'])
                try:
                    self.returncode = deploy.worker(outer.output)
                except RuntimeError:
                    self.returncode = 1
                lease = outer.output / 'lease'
                lease.mkdir()
                status = dict(status='COMPLETED' if self.returncode == 0 else
                              'COMMAND_FAILED', child_returncode=self.returncode,
                              compute_backend='nvml', poll_interval_seconds=1.0,
                              external_lease=True, finished=deploy.stamp())
                if guard_cleanup_error:
                    status.update(status='ENV_ABORT', cleanup_error='unknown descendant')
                    self.returncode = 75
                deploy.save(lease / 'exit.json', status)
                (lease / 'nvml-queries.jsonl').write_text('{"status":"OK"}\n')
            def wait(self, timeout):
                self.assert_timeout = timeout
                return self.returncode
            def poll(self):
                return self.returncode
        with patch.object(deploy, 'require_guard'), \
             patch.object(deploy, 'verify_tools'), \
             patch.object(deploy, 'freeze_tools', side_effect=lambda out:
                          deploy.save(out / 'sources.json', {})), \
             patch.object(deploy, 'supervise_health', side_effect=health), \
             patch.object(deploy.subprocess, 'Popen', side_effect=GuardProcess):
            rc = deploy.deploy(self.manifest_path, self.audit_path,
                               self.output, self.package)
        return rc, calls, deploy.read(self.output / 'deployment.json')

    def test_single_guard_two_health_stages_deploys(self):
        rc, calls, result = self.run_fake_manager()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, list(deploy.HEALTH))
        self.assertEqual(result['status'], 'DEPLOYED_HEALTH_CHECKED')
        self.assertEqual((result['loads'], result['requests']), (2, 2))
        self.assertFalse(deploy.recovery_path().exists())
        deploy.verify_seal(self.output)

    def test_failed_health_automatically_rolls_back(self):
        rc, calls, result = self.run_fake_manager(failed_health=True)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, list(deploy.HEALTH))
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['rollback']['status'], 'ROLLED_BACK')
        self.assertFalse(deploy.recovery_path().exists())
        self.assert_old()

    def test_unknown_guard_cleanup_retains_recovery_gate(self):
        rc, _, result = self.run_fake_manager(guard_cleanup_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(result['status'], 'RECOVERY_REQUIRED')
        self.assertIn('guard_cleanup_error', result)
        self.assertTrue(deploy.recovery_path().exists())
        self.assert_old()

    def test_standalone_rollback_preserves_original_deployment_seal(self):
        self.run_fake_manager()
        original = deploy.identity(self.output / 'sealed-files.json')
        self.assertEqual(deploy.rollback_entry(self.output), 0)
        self.assertEqual(deploy.verify_seal(self.output), original)
        self.assertEqual(len(list(self.base.glob('deploy-v1-rollback-*'))), 1)
        self.assert_old()


if __name__ == '__main__':
    unittest.main(verbosity=2)
