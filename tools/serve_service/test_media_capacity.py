"""CPU contracts for native managed media; synthetic fixtures are not GPU proof."""
import copy
import unittest

from tools.serve_ops import contracts, fixtures
from tools.serve_service import capacity, package, supervisor
from tools.serve_service import test_capacity as previous
from tools.serve_recovery import profile_contract, profile_evidence
from tools.serve_recovery import test_native_bundle, test_profile_integration


class MediaCapacityTests(unittest.TestCase):
    setUp = previous.CapacityTests.setUp
    fixture = previous.CapacityTests.fixture
    sealed_command = previous.CapacityTests.sealed_command

    def test_v2_explicit_media_and_old_v1_identity(self):
        old = capacity.normalize(dict(version=1))
        self.assertEqual(old['name'], capacity.NAME)
        self.assertNotIn('allow_media', old)
        for enabled in (False, True):
            selected = capacity.normalize(dict(version=2, allow_media=enabled))
            self.assertEqual(selected['name'], capacity.NATIVE_NAME)
            self.assertIs(selected['allow_media'], enabled)
            self.assertEqual({k:v for k,v in selected.items() if k not in ('name','allow_media')},
                             {k:v for k,v in old.items() if k != 'name'})
        self.assertFalse(capacity.normalize(dict(version=2))['allow_media'])
        for bad in [dict(version=1, allow_media=True), dict(version=2, allow_media=1),
                    dict(version=2, allow_media=None), dict(version=2, allow_media='false'),
                    dict(version=2, allow_media=True, max_seq=2)]:
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                capacity.normalize(bad)
        bad = capacity.normalize(dict(version=2));bad['name'] = capacity.NAME
        with self.assertRaises(RuntimeError):capacity.validate(bad)

    def test_media_observation_missing_disabled_or_coerced_fails_closed(self):
        for enabled in (False, True):
            selected = capacity.normalize(dict(version=2, allow_media=enabled))
            observed = dict(supervisor.expected_configuration(selected),
                default_output_tokens=selected['max_tokens'],
                request_deadline_ms=selected['request_deadline_ms'], mtp_k=3,
                startup_memory_headroom_mib=selected['startup_memory_headroom_mib'])
            probe = dict(process=dict(executable_sha256='binary', listener_scope='loopback'),
                protocol='protocol', configuration=observed, ready=True,
                metrics=None, health_status='healthy')
            self.assertEqual(supervisor.classify(probe,'protocol','binary',profile=selected), 'READY')
            for invalid in (None, not enabled, int(enabled)):
                bad = copy.deepcopy(probe)
                if invalid is None:bad['configuration'].pop('media_enabled')
                else:bad['configuration']['media_enabled'] = invalid
                with self.assertRaises(RuntimeError):
                    supervisor.classify(bad,'protocol','binary',profile=selected)
                with self.assertRaises(RuntimeError):
                    capacity.observation(selected,bad['configuration'])
        value = fixtures.configuration()
        for enabled in (False, True):
            value['runtime']['media_enabled'] = enabled
            self.assertIs(contracts.configuration(value)['media_enabled'], enabled)
        for invalid in (0,1,None,'true'):
            value['runtime']['media_enabled'] = invalid
            with self.assertRaises(contracts.InvalidObservation):contracts.configuration(value)
        del value['runtime']['media_enabled']
        self.assertNotIn('media_enabled',contracts.configuration(value))

    def media_fixture(self):
        binary, release, model = self.fixture()
        metadata = package.read(release)
        directory = self.root/'declarations'
        for filename in ('capabilities.json','configuration.json'):
            value = package.read(directory/filename)
            value['runtime'] = dict(media_enabled=True)
            package.save(directory/filename,value)
            metadata['release_declarations']['files'][filename] = package.sha(directory/filename)
        metadata['chat_protocol']['runtime'] = dict(media_enabled=True)
        metadata['observed_configuration'] = package.read(directory/'configuration.json')
        package.save(release,metadata)
        return binary, release, model

    def test_sealed_native_package_emits_media_flag_and_keeps_text_mtp(self):
        binary, release, model = self.media_fixture()
        receipt = package.prepare(binary,release,model,self.root/'package',
            nvml_policy='degrade',capacity_config=dict(version=2,allow_media=True))
        args = self.sealed_command(receipt)
        self.assertEqual(args.count('--allow-media'),1)
        self.assertIn('--mtp',args)
        self.assertNotIn('True',args)
        self.assertEqual(args[args.index('--max-seq')+1],'1')

    def test_media_requires_observed_capability_before_packaging_and_when_verifying(self):
        binary, release, model = self.fixture()
        target = self.root/'package'
        with self.assertRaisesRegex(RuntimeError,'declare enabled media'):
            package.prepare(binary,release,model,target,nvml_policy='degrade',
                capacity_config=dict(version=2,allow_media=True))
        self.assertFalse(target.exists())
        receipt = package.prepare(binary,release,model,target,nvml_policy='degrade',
            capacity_config=dict(version=2,allow_media=False))
        self.assertNotIn('--allow-media',self.sealed_command(receipt))
        manifest = package.read(target/'manifest.json')
        manifest['settings']['profile']['allow_media'] = True
        package.save(target/'manifest.json',manifest)
        with self.assertRaisesRegex(RuntimeError,'declare enabled media'):
            package.verify(target,package.sha(target/'manifest.json'))

    def test_light_profile_preserves_media_and_declared_application_features(self):
        primary = capacity.normalize(dict(version=2,allow_media=True))
        pair = profile_contract.profiles(primary)
        self.assertTrue(pair['light']['allow_media'])
        self.assertFalse(pair['light']['mtp_enabled'])
        self.assertEqual(profile_contract.profile_pair(pair),pair)
        bad = copy.deepcopy(pair);bad['light']['allow_media'] = False
        with self.assertRaises(ValueError):profile_contract.profile_pair(bad)
        first = dict(runtime=dict(mtp_enabled=True,media_enabled=True))
        second = dict(runtime=dict(mtp_enabled=False,media_enabled=True))
        profile_contract.capability_pair(first,second)
        second['runtime']['media_enabled'] = False
        with self.assertRaises(ValueError):profile_contract.capability_pair(first,second)


class MediaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _,self.registry,self.records = test_profile_integration.evidence_files(self.fixture)
        selected = self.registry['profiles']['primary']
        selected = dict(selected,name=capacity.NATIVE_NAME,allow_media=True)
        self.registry['profiles'] = profile_contract.profiles(selected)
        raw = (test_profile_integration.ROOT/profile_evidence.MEDIA_FILE).read_bytes()
        self.media = dict(request=profile_evidence.media_request(raw),
            response=dict(status='completed',usage=dict(input_tokens=100),output=[
                dict(type='message',role='assistant',content=[dict(type='output_text',
                    text='{"start":"red","end":"blue"}')])]))
        for name,record in self.records.items():
            record.update(format=profile_evidence.MEDIA_FORMAT,
                capacity=copy.deepcopy(self.registry['profiles'][name]))
            record['configuration']['media_enabled'] = True
            record['capabilities']['runtime']['media_enabled'] = True
            record['business']['media'] = copy.deepcopy(self.media)
            self.registry['qualifications'][name] = profile_contract.qualification(name,
                record['capacity'],record['identity'],record['capabilities'],
                record['configuration'],record['startup_memory'],'f'*64 if name=='primary' else 'e'*64)

    def test_visual_qualification_requires_both_real_source_and_result(self):
        for name in ('primary','light'):
            digest = 'f'*64 if name=='primary' else 'e'*64
            profile_evidence.validate(self.records[name],self.registry,name,digest)
            for mutate in [lambda r:r['business'].pop('media'),
                lambda r:r.update(format=profile_evidence.FORMAT),
                lambda r:r['business']['media'].update(response=r['business']['schema']),
                lambda r:r['business']['media']['request']['input'][0]['content'][0].update(file_data=''),
                lambda r:r['business']['media']['response']['output'][0]['content'][0].update(text='{"start":"blue","end":"red"}')]:
                bad = copy.deepcopy(self.records[name]);mutate(bad)
                with self.assertRaises(ValueError):
                    profile_evidence.validate(bad,self.registry,name,digest)

    def test_media_capability_and_effective_configuration_must_agree(self):
        record = self.records['primary']
        bad = copy.deepcopy(record['capabilities']);bad['runtime']['media_enabled'] = False
        with self.assertRaisesRegex(ValueError,'media capability'):
            profile_contract.qualification('primary',record['capacity'],record['identity'],bad,
                record['configuration'],record['startup_memory'],'f'*64)


if __name__ == '__main__':unittest.main()
