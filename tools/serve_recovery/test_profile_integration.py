"""CPU package/state/admission integration. All qualification data is synthetic."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from tools.serve_gateway import client_policy
from tools.serve_recovery import access_store, binding, bundle, controller, deployment
from tools.serve_recovery import gateway, initialization, ledger, profile_contract
from tools.serve_recovery import profile_controller, profile_evidence, profile_manager
from tools.serve_recovery import profile_policy, profile_runtime, profile_store, service
from tools.serve_recovery import test_gateway, test_native_bundle, test_profile_contract
from tools.serve_service import capacity, supervisor

ROOT = Path(__file__).resolve().parents[2]


def evidence_files(fixture):
    """CPU representation only; temporary fake engine cannot qualify a release."""
    reference = test_profile_contract.ProfileContractTests(); reference.setUp()
    base = fixture.base
    base['version'] = 11
    base['client_policy'] = {}  # The synthetic engine still has the real CLI policy slot.
    base['model_files'] = {n: 'a'*64 for n in
                          ('config.json', 'tokenizer.json', 'tokenizer_config.json')}
    base['settings']['profile'] = capacity.normalize(dict(version=1, max_tokens=384))
    profiles = profile_contract.profiles(base['settings']['profile'])
    fixed = profile_contract.binding(base['files']['q4t'], base['protocol_sha256'],
        base['model_files'], bundle.assets(fixture.model))
    source = dict(path='docs/SERVE_MEMORY_STOP_2026-11-03.md', content='CPU fixture only\n')
    digest = hashlib.sha256(source['content'].encode()).hexdigest()
    def response(content, finish='stop', **fields):
        return dict(choices=[dict(finish_reason=finish,
            message=dict(role='assistant', content=content, **fields))])
    business = dict(schema=response('{"answer":42}'),
        tool_call=response(None, 'tool_calls', tool_calls=[dict(id='call_cpu', type='function',
            function=dict(name='project_document', arguments=json.dumps({'path':source['path']})))]),
        tool_result=response(digest[:12]), tool_source=source, embedding=None,
        tool_reply=dict(role='tool', tool_call_id='call_cpu', content=json.dumps(
            dict(path=source['path'], sha256=digest, answer=digest[:12]))))
    directory = fixture.directory/'qualifications'; directory.mkdir(exist_ok=True)
    records, receipts = {}, {}
    for name, profile in profiles.items():
        caps = copy.deepcopy(reference.caps)
        caps['runtime']['mtp_enabled'] = profile['mtp_enabled']
        caps['protocol_source_sha256'] = fixed['protocol_sha256']
        observed = dict(reference.observed, mtp_enabled=profile['mtp_enabled'],
                        mtp_k=3 if profile['mtp_enabled'] else None)
        needed = (100 if name == 'primary' else 90)*1024**2
        memory = dict(state='admitted', estimate_bytes=needed-1024**2,
            headroom_bytes=1024**2, required_bytes=needed,
            observed_total_bytes=128*1024**3, observed_available_bytes=120*1024**3)
        record = dict(format=profile_evidence.FORMAT, profile=name, identity=fixed,
            capacity=profile, capabilities=caps, configuration=observed,
            startup_memory=memory, business=copy.deepcopy(business),
            environment=dict(status='PASS', env_abort=False, all_queries_ok=True,
                nvml_queries=1, lease_result_sha256='a'*64, cleanup_complete=True))
        path = directory/(name+'.json'); bundle.engine.save(path, record)
        receipts[name] = profile_contract.qualification(name, profile, fixed, caps,
            observed, memory, bundle.engine.sha(path))
        records[name] = record
    registry = dict(format=profile_contract.FORMAT, identity=fixed, profiles=profiles,
                    qualifications=receipts)
    path = directory/'registry.json'; bundle.engine.save(path, registry)
    return path, registry, records


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path, self.registry, self.records = evidence_files(self.fixture)

    def prepare(self):
        receipt = self.fixture.prepare(recovery_profiles=self.path)
        return Path(receipt['package']), receipt['manifest_sha256']

    def test_sealed_qualification_and_independent_ingress_bind_same_capacity(self):
        package, digest = self.prepare()
        manifest = bundle.verify(package, digest)
        self.assertEqual(manifest['version'], 15)
        self.assertEqual(manifest['recovery_profiles'], self.registry)
        config = bundle.gateway_config_for(package, manifest, self.fixture.directory)
        self.assertEqual(config['recovery_profiles'], self.registry)
        for name in profile_contract.NAMES:
            self.assertEqual(bundle.engine.read(package/('profile-'+name+'.json')), self.records[name])
        self.assertEqual(manifest['recovery_profile_policy'], profile_policy.DEFAULTS)

    def test_resigning_incomplete_business_or_environment_does_not_qualify(self):
        for mutate in [lambda v: v['business']['tool_reply'].update(tool_call_id='foreign'),
                       lambda v: v['business'].update(tool_result=v['business']['schema']),
                       lambda v: v['environment'].update(env_abort=True),
                       lambda v: v['environment'].update(cleanup_complete=False)]:
            bad = copy.deepcopy(self.records['light']); mutate(bad)
            registry = copy.deepcopy(self.registry)
            registry['qualifications']['light']['evidence_sha256'] = 'f'*64
            with self.assertRaises(ValueError):
                profile_evidence.validate(bad, registry, 'light', 'f'*64)

    def test_fresh_initialization_cli_status_and_lost_descriptor_keep_original_journals(self):
        package, digest = self.prepare()
        manifest = bundle.verify(package, digest)
        state = self.fixture.directory/'state'
        with mock.patch.object(deployment, 'write_descriptor', side_effect=OSError('before publication')):
            with self.assertRaises(OSError): service.initialize(package, digest, state)
        preserved = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob('policy.json')}
        core = (state/'ledger.json').read_bytes()
        initialization.finish(state, package, digest, manifest)
        self.assertEqual(core, (state/'ledger.json').read_bytes())
        for name, raw in preserved.items(): self.assertEqual(raw, (state/name).read_bytes())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = service.main(['profiles-status', '--state', str(state)], package=package, digest=digest)
        self.assertEqual(status, 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value['enabled']); self.assertEqual(value['selected'], 'primary')
        self.assertFalse(value['model_actions'])
        (state/'profile-selection/policy.json').unlink()
        with self.assertRaises((OSError, ValueError)):
            initialization.finish(state, package, digest, manifest)
        self.assertFalse((state/'profile-selection/policy.json').exists())

    def test_interrupted_enablement_keeps_keys_usage_budget_and_resumes_one_profile_seed(self):
        old_receipt = self.fixture.prepare()
        old = self.fixture.directory/'old-package'
        shutil.copytree(old_receipt['package'], old)
        shutil.rmtree(old_receipt['package'])
        state = self.fixture.directory/'state'
        service.initialize(old, old_receipt['manifest_sha256'], state)
        paths = ['ledger.json', *(w.directory_name+'/policy.json' for w in deployment.FAMILIES.values())]
        before = {n:(state/n).read_bytes() for n in paths}
        package, digest = self.prepare(); request = str(uuid.uuid4())
        with mock.patch.object(deployment, 'write_descriptor', side_effect=OSError('upgrade interrupted')):
            with self.assertRaises(OSError):
                deployment.switch(state, package, digest,
                    expected=old_receipt['manifest_sha256'], request_id=request)
        seeded = (state/'profile-selection/policy.json').read_bytes()
        deployment.switch(state, package, digest, expected=old_receipt['manifest_sha256'], request_id=request)
        self.assertEqual(seeded, (state/'profile-selection/policy.json').read_bytes())
        for name, raw in before.items(): self.assertEqual(raw, (state/name).read_bytes())
        with self.assertRaisesRegex(ValueError, 'discard qualified profile state'):
            deployment.switch(state, old, old_receipt['manifest_sha256'],
                              expected=digest, request_id=str(uuid.uuid4()), rollback=True)

    def admission(self, selected, *, observed=None, fingerprint=None):
        cfg = test_gateway.config(self.fixture.directory)
        cfg.update(version=2, native_ingress=cfg.pop('nginx'),
            native_ingress_sha256=cfg.pop('nginx_sha256'),
            binary_sha256=self.registry['identity']['binary_sha256'],
            protocol_sha256=self.registry['identity']['protocol_sha256'],
            capacity_profile=self.registry['profiles']['primary'], recovery_profiles=self.registry)
        snap = test_gateway.snapshot(cfg)
        profile = self.registry['profiles'][selected]
        snap['binding']['capacity_sha256'] = fingerprint or ledger.digest(profile)
        raw = binding.policy_bytes(cfg['backend_policy'], snap['binding']['gateway_token'])
        process = dict(executable_sha256=cfg['binary_sha256'], listener_scope='loopback')
        configuration = dict(self.records[selected]['configuration'], operations_port=18183)
        if observed: configuration.update(observed)
        value = dict(process=process, operations_process=process, protocol=cfg['protocol_sha256'],
            configuration=configuration, client_policy=dict(client_policy.binding(raw),
                profile='q4t-gateway-clients-v1', credential_values_redacted=True, mutable=False),
            operations_port=18183, observability_version=5, ready=True, health_status='healthy', metrics=None)
        channel = mock.Mock(service_id=str(uuid.uuid4()))
        gate = gateway.Admission(cfg, channel)
        try:
            with mock.patch.object(gateway.control, 'binding', return_value=snap), \
                 mock.patch.object(supervisor, 'probe', return_value=value):
                gate.update()
                return gate.cached is not None, channel
        finally: gate.close()

    def test_real_classifier_accepts_only_bound_primary_or_light_configuration(self):
        for selected in profile_contract.NAMES:
            ready, channel = self.admission(selected)
            self.assertTrue(ready); channel.bound.assert_called_once_with(1)
        for changed in [dict(mtp_enabled=True), dict(max_context_tokens=4096),
                        dict(default_output_tokens=256), dict(structured_backend=False)]:
            ready, channel = self.admission('light', observed=changed)
            self.assertFalse(ready); channel.bound.assert_not_called()
        ready, channel = self.admission('light', fingerprint='f'*64)
        self.assertFalse(ready); channel.bound.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _, self.registry, _ = evidence_files(self.fixture)
        self.state = self.fixture.directory/'state'; self.state.mkdir(mode=0o700)
        self.binding = access_store.binding(self.state, str(uuid.uuid4()))
        self.boot = supervisor.process_identity(os.getpid())['boot_id']
        self.now = ledger.SECOND
        profile_store.initialize_bound(self.binding,
            profile_policy.initial(self.registry, ledger.Stamp(self.boot, self.now)))
        self.runtime = profile_runtime.Selection(self.binding, self.registry, self.boot,
                                                 nanos=lambda:self.now)
        self.addCleanup(self.close_runtime)

    def close_runtime(self):
        end = time.monotonic()+3
        while not self.runtime.close() and time.monotonic() < end: time.sleep(.01)
        self.assertIsNone(self.runtime.worker.process)

    def until(self, predicate, seconds=4):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            self.runtime.step()
            if predicate(): return
            time.sleep(.01)
        self.fail('runtime deadline: '+str(self.runtime.status()))

    def test_new_helper_must_durably_resume_before_switch_eligibility(self):
        self.until(self.runtime.ready)
        self.assertEqual(self.runtime.state['revision'], 1)
        self.assertTrue(self.runtime.prepare('light', 'cold_memory_capacity', 1))
        self.assertFalse(self.runtime.ready())
        self.until(self.runtime.ready)
        before = copy.deepcopy(self.runtime.state)
        self.runtime.worker.process.kill()
        self.until(lambda:self.runtime.worker.process is None)
        self.now += 2*ledger.DAY
        self.until(self.runtime.ready)
        self.assertEqual(self.runtime.state['history'], before['history'])
        self.assertEqual(self.runtime.state['pending'], before['pending'])
        self.assertEqual(self.runtime.state['clock']['logical_ns'], before['clock']['logical_ns'])
        self.assertGreater(self.runtime.state['revision'], before['revision'])

    def test_unknown_commit_never_authorizes_switch_and_external_file_is_not_reset(self):
        self.until(self.runtime.ready)
        path = self.state/'profile-selection/policy.json'
        saved = path.read_bytes(); path.write_bytes(b'{')
        self.assertTrue(self.runtime.prepare('light', 'cold_memory_capacity', 1))
        self.until(lambda:self.runtime.worker.error == 'state_invalid')
        self.assertFalse(self.runtime.ready())
        self.assertIsNone(self.runtime.state['pending'])
        self.assertEqual(path.read_bytes(), b'{')
        path.write_bytes(saved); self.runtime.worker.next_read = 0
        self.until(self.runtime.ready)
        self.assertEqual(self.runtime.state['pending']['target'], 'light')

    def test_controller_parks_io_before_launch_and_confirms_only_new_live_generation(self):
        actor = SimpleNamespace(base=copy.deepcopy(self.fixture.base),
            manifest=dict(model_assets=self.registry['identity']['model_assets'],
                          recovery_profile_policy=profile_policy.DEFAULTS),
            phase='READY', reason='serving', grant=dict(generation=1))
        profiles = profile_controller.Profiles(actor, self.registry, self.binding)
        self.runtime.close()  # It has not started; use the actual controller's coordinator.
        self.runtime = profiles.selection
        self.runtime.nanos = lambda:self.now
        self.until(self.runtime.ready)
        observation = dict(total_bytes=128*1024**3, available_bytes=120*1024**3)
        self.assertFalse(profiles.prepare_launch(actor, observation))
        end = time.monotonic()+3
        while not profiles.prepare_launch(actor, observation):
            self.assertLess(time.monotonic(), end); time.sleep(.01)
        self.assertIsNone(self.runtime.worker.process)
        self.assertEqual(actor.settings['profile'], self.registry['profiles']['primary'])
        profiles.started(1); self.until(self.runtime.ready)
        actor.watchdog = mock.Mock()
        for second in range(1, 32):
            self.now = second*ledger.SECOND
            actor.watchdog.snapshot.return_value = dict(state='OPEN', sampled_monotonic=second,
                total_bytes=128*1024**3, available_bytes=7*1024**3)
            self.assertFalse(profiles.switch_ready(actor))
        self.assertFalse(self.runtime.ready())
        self.until(self.runtime.ready)
        self.assertTrue(profiles.switch_ready(actor))
        profiles.retired()
        # Simulate completed old-tree cleanup, then use the original durable intent.
        self.runtime.unpark(); self.until(self.runtime.ready)
        end = time.monotonic()+3
        while not profiles.prepare_launch(actor, observation):
            self.assertLess(time.monotonic(), end); time.sleep(.01)
        self.assertEqual(profiles.launch, 'light')
        self.assertIsNone(self.runtime.worker.process)
        command = controller.model_command('/cpu-package', actor.base,
            dict(state_directory=str(self.state)), self.state/'client-policy.json')
        self.assertIn('--no-mtp', command); self.assertNotIn('--mtp', command)
        actor.grant['generation'] = 2; profiles.started(2)
        # Four isolated I/O sessions stand for two real model launches. Advance
        # only that worker's restart window; no profile cooldown credit is added.
        self.runtime.worker.clock = lambda: time.monotonic()+61
        self.until(self.runtime.ready)
        self.assertFalse(profiles.switch_ready(actor))
        self.until(self.runtime.ready)
        self.assertEqual(self.runtime.state['selected'], 'light')
        self.assertIsNone(self.runtime.state['pending'])
        self.assertEqual(len(self.runtime.state['history']), 1)


if __name__ == '__main__': unittest.main()
