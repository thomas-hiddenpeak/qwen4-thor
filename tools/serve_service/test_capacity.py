"""CPU acceptance for configurable, source-bound service capacity."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tools.serve_ops import contracts, fixtures
from tools.serve_service import capacity, package as pkg, supervisor as sup
from tools.serve_service import test_service as old


class CapacityTests(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture
    run_supervisor = old.Case.run_supervisor

    def profile(self, **changes):
        return capacity.normalize(dict(version=1, max_len=4096,
            max_prefill=1024, max_tokens=128, max_inflight_requests=4,
            max_queued_requests=2, request_deadline_ms=30000, **changes))

    def observation(self, pid=42, ready=True):
        p = self.selected
        cfg = dict(sup.expected_configuration(p), default_output_tokens=p['max_tokens'],
            request_deadline_ms=p['request_deadline_ms'],
            mtp_k=p['mtp_k'] if p['mtp_enabled'] else None,
            startup_memory_headroom_mib=p['startup_memory_headroom_mib'])
        return dict(process=dict(pid=pid, executable_sha256='binary', listener_scope='loopback'),
            protocol='protocol', ready=ready, metrics=None, health_status='healthy', configuration=cfg)

    def test_omitted_fields_preserve_defaults_without_mutating_input(self):
        source = dict(version=1, max_len=4096)
        p = capacity.normalize(source)
        self.assertEqual(source, dict(version=1, max_len=4096))
        for key in pkg.PROFILE.keys()-{'name', 'max_len'}:
            self.assertEqual(p[key], pkg.PROFILE[key])
        self.assertEqual(p['max_len'], 4096)
        p['max_seq'] = 5
        self.assertEqual(pkg.PROFILE['max_seq'], 1)

    def test_unknown_fields_types_and_all_numeric_boundaries_rejected(self):
        for source in ([], {}, {'version': True}, {'version': 2},
                       {'version': 1, 'host': '0.0.0.0'}, {'version': 1, 'mtp_enabled': 1}):
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                capacity.normalize(source)
        for key, (lo, hi) in capacity.BOUNDS.items():
            for value in (True, 1.0, '2', lo-1, hi+1):
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                    capacity.normalize(dict(version=1, **{key: value}))
        for source in (dict(version=1, max_seq=3, max_prefill=2),
                       dict(version=1, max_inflight_requests=2, max_queued_requests=3)):
            with self.assertRaises(RuntimeError):capacity.normalize(source)

    def test_command_uses_selected_profile_and_explicit_disabled_switches(self):
        self.selected = self.profile(mtp_enabled=False, min_available_memory_mib=0,
                                     startup_memory_headroom_mib=0)
        cfg = pkg.settings(18182, 0, capacity_profile=self.selected)
        args = pkg.command('/private/package', dict(model_dir='/readonly/model', settings=cfg))
        self.assertIn('--no-mtp', args)
        self.assertNotIn('--mtp', args)
        self.assertNotIn('--mtp-enabled', args)
        for key in capacity.BOUNDS:
            self.assertEqual(args[args.index('--'+key.replace('_', '-'))+1], str(self.selected[key]))
        observed = self.observation()
        self.assertFalse(observed['configuration']['memory_enabled'])
        self.assertEqual(observed['configuration']['memory_recovery_mib'], 0)
        self.assertEqual(sup.classify(observed, 'protocol', 'binary', profile=self.selected), 'READY')

    def test_selected_observation_rejects_fixed_defaults_missing_and_wrong_types(self):
        self.selected = self.profile()
        value = self.observation()
        self.assertEqual(sup.classify(value, 'protocol', 'binary', profile=self.selected), 'READY')
        for field in value['configuration']:
            for replacement in ('missing', 'different'):
                bad = copy.deepcopy(value)
                if replacement == 'missing':del bad['configuration'][field]
                else:bad['configuration'][field] = False
                with self.subTest(field=field, replacement=replacement), self.assertRaises(RuntimeError):
                    sup.classify(bad, 'protocol', 'binary', profile=self.selected)
        old_value = old.Case.observation(self)
        with self.assertRaises(RuntimeError):
            sup.classify(old_value, 'protocol', 'binary', profile=self.selected)
        value['health_status'] = 'unhealthy'
        self.assertEqual(sup.classify(value, 'protocol', 'binary', profile=self.selected), 'BACKEND_UNHEALTHY')

    def test_real_archived_configuration_projection_includes_capacity_fields(self):
        value = fixtures.configuration()
        value['default_output_tokens'] = dict(effective=128)
        value['runtime'].update(mtp_enabled=True, mtp_k=3)
        value['startup_memory'] = dict(headroom_mib=dict(effective=2048))
        projected = contracts.configuration(value)
        self.assertEqual((projected['default_output_tokens'], projected['mtp_k'],
                          projected['startup_memory_headroom_mib']), (128, 3, 2048))
        value['runtime']['mtp_enabled'] = False
        with self.assertRaises(contracts.InvalidObservation):contracts.configuration(value)
        value['runtime']['mtp_k'] = None
        self.assertIsNone(contracts.configuration(value)['mtp_k'])
        for parent, name in ((value['default_output_tokens'], 'effective'),
                             (value['startup_memory']['headroom_mib'], 'effective')):
            previous = parent[name];parent[name] = True
            with self.assertRaises(contracts.InvalidObservation):contracts.configuration(value)
            parent[name] = previous

    def test_fair_queue_matches_durable_policy_and_rejects_overcommit(self):
        from tools.serve_recovery import quota_policy
        from tools.serve_recovery.test_gateway import config
        clients = config(self.root)['backend_policy']
        clients.update(version=2, fair_queue=True)
        for row in clients['clients']:
            row.update(max_inflight=2, requests_per_minute=0, tokens_per_minute=0)
        p = self.profile()
        capacity.check_clients(p, clients)
        durable = quota_policy.from_base(clients)
        quota_policy.capacity(durable, 4, 2)
        for inflight, queued in ((3, 2), (4, 1)):
            with self.assertRaises(RuntimeError):
                capacity.check_clients(dict(p, max_inflight_requests=inflight, max_queued_requests=queued), clients)
            with self.assertRaises(ValueError):quota_policy.capacity(durable, inflight, queued)

    def sealed_command(self, receipt):
        code = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
            'from tools.serve_service import package as p;'
            'm=p.verify(sys.argv[2],sys.argv[3]);print(json.dumps(p.command(sys.argv[2],m)))')
        target = Path(receipt['package'])
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(target/'source'),
            str(target), receipt['manifest_sha256']], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_custom_package_executes_its_own_verifier_without_checkout_modules(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            nvml_policy='degrade', capacity_config=dict(version=1, max_tokens=99, mtp_enabled=False))
        value = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['version'], 11)
        self.assertIn('source/tools/serve_service/capacity.py', value['files'])
        args = self.sealed_command(receipt)
        self.assertIn('--no-mtp', args)
        self.assertEqual(args[args.index('--max-tokens')+1], '99')

    def test_old_package_needs_no_new_module_and_rejects_boolean_config_alias(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package', nvml_policy='degrade')
        target = Path(receipt['package'])
        self.assertFalse((target/'source/tools/serve_service/capacity.py').exists())
        self.assertIn('--mtp', self.sealed_command(receipt))
        value = pkg.read(target/'manifest.json')
        value['settings']['profile']['max_seq'] = True
        pkg.save(target/'manifest.json', value)
        with self.assertRaisesRegex(RuntimeError, 'unknown managed configuration'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_malformed_capacity_fails_before_package_creation(self):
        binary, release, model = self.fixture()
        target = self.root/'package'
        with self.assertRaises(RuntimeError):
            pkg.prepare(binary, release, model, target, capacity_config=dict(version=1, mtp_enabled=1))
        self.assertFalse(target.exists())

    def test_configurable_profile_cannot_masquerade_as_historical_version(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            nvml_policy='degrade', capacity_config=dict(version=1))
        target = Path(receipt['package']);value = pkg.read(target/'manifest.json')
        value['version'] = 9
        pkg.save(target/'manifest.json', value)
        with self.assertRaisesRegex(RuntimeError, 'unknown managed configuration'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_optional_host_resources_remain_bound_to_capacity_package(self):
        from tools.serve_service import resources
        binary, release, model = self.fixture()
        selected = resources.policy(32, 64, 8)
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            nvml_policy='degrade', host_resources=selected, capacity_config=dict(version=1))
        value = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(value['version'], 11)
        self.assertEqual(value['host_resources'], selected)
        self.assertIn('--mtp', self.sealed_command(receipt))

    def test_supervisor_uses_custom_profile_with_same_real_cpu_child(self):
        self.selected = self.profile()
        seen = []
        def observe(pid, _port):
            seen.append(pid)
            if len(seen) == 3:pkg.save(self.root/'runtime/stop.request.json', {})
            return self.observation(pid)
        rc, state = self.run_supervisor(observe, isolated=True,
            config_overrides=dict(profile=self.selected), manifest_overrides=dict(version=11))
        self.assertEqual(rc, 0)
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(state['cleanup_confirmed'])
        self.assertEqual((state['attempts'], state['restarts']), (1, 0))


if __name__ == '__main__':
    unittest.main()
