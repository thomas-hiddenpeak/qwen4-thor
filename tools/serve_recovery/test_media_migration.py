"""CPU-only media migration: explicit extension, capability-preserving recovery."""
import copy
import unittest

from tools.serve_recovery import profile_contract, profile_evidence, profile_migration
from tools.serve_recovery import profile_policy
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_service import capacity


class MediaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.base=previous.MigrationTests();self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def extension(self):
        registry=copy.deepcopy(self.base.new)
        registry['profiles']=profile_contract.profiles(dict(registry['profiles']['primary'],
            name=capacity.NATIVE_NAME,allow_media=True))
        raw=(previous.test_profile_integration.ROOT/profile_evidence.MEDIA_FILE).read_bytes()
        proof=dict(request=profile_evidence.media_request(raw),response=dict(status='completed',
            usage=dict(input_tokens=100),output=[dict(type='message',role='assistant',content=[
                dict(type='output_text',text='{"start":"red","end":"blue"}')])]))
        records={name:copy.deepcopy(row['record']) for name,row in self.base.second.items()}
        for record in records.values():
            record['format']=profile_evidence.MEDIA_FORMAT
            record['configuration']['media_enabled']=True
            record['capabilities']['runtime']['media_enabled']=True
            record['capabilities']['media']['attention_shared_bytes']=48128
            record['capabilities'].setdefault('file_inputs',{})['native_video']=dict(
                profile='q4t-native-video-input-v1',model_media_enabled=True,
                requires_mtp_disabled=False,decode_strategy='target_model')
            record['business']['media']=copy.deepcopy(proof)
        return registry,previous.bind_records(registry,records)

    def cache_extension(self):
        registry, evidence = self.extension()
        old = copy.deepcopy(registry)
        records = {n: copy.deepcopy(v['record']) for n, v in evidence.items()}
        for record in records.values():
            caps = record['capabilities']
            caps['media'].update(state_cache=False, chunked_prefill=False)
            for name in ('chat', 'responses'):
                caps.setdefault(name, {})['prompt_cache'] = dict(
                    media_fingerprint_pending=True)
        first = previous.bind_records(old, records)
        target = copy.deepcopy(old)
        target['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        records = copy.deepcopy(records)
        for record in records.values():
            caps = record['capabilities']
            caps['media'].update(state_cache=True, chunked_prefill=True,
                cache_identity='source_bytes_processor_geometry_and_position',
                cache_layout='target_with_three_axis_rope_and_delta',
                cache_boundaries='complete_visual_blocks')
            for name in ('chat', 'responses'):
                caps[name]['prompt_cache']['media_fingerprint_pending'] = False
        return old, target, first, previous.bind_records(target, records)

    def test_media_cache_requires_explicit_upgrade_and_cannot_lose_or_auto_add(self):
        old, new, first, second = self.cache_extension()
        saved = copy.deepcopy((first, second))
        def permit(a, b, x, y, **kwargs):
            return profile_migration.permit(a, b, x, y, self.base.config,
                                            self.base.config, **kwargs)
        for kwargs in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError): permit(old, new, first, second, **kwargs)
        grant = permit(old, new, first, second, allow_capability_extension=True)
        self.assertEqual(grant['format'], profile_migration.EXTENSION_FORMAT)
        for automatic in (False, True):
            with self.assertRaises(ValueError):
                permit(new, old, second, first, automatic=automatic,
                       allow_capability_extension=True)
        self.assertTrue(permit(new, new, second, second, automatic=True)['automatic'])
        self.assertEqual((first, second), saved)

    def test_media_cache_upgrade_keeps_pending_and_consumed_budget(self):
        old, new, first, second = self.cache_extension()
        initial = profile_policy.initial(old, self.base.stamp(0), self.base.config)
        pending = profile_policy.prepare(initial, 'light', 'sustained_memory_pressure',
                                         str(previous.uuid.uuid4()), 7, self.base.stamp(1))
        grant = profile_migration.permit(old, new, first, second, self.base.config,
            self.base.config, allow_capability_extension=True)
        after = profile_migration.candidate(pending, grant, self.base.stamp(1))
        for key in ('pending', 'history', 'selected', 'not_before_ns', 'light_since_ns',
                    'configuration', 'profiles'):
            self.assertEqual(after[key], pending[key])

    def test_media_cache_rejects_inconsistent_fingerprints_layout_and_boolean_types(self):
        _, _, _, second = self.cache_extension()
        caps = second['primary']['record']['capabilities']
        for change in (lambda c:c['media'].update(state_cache=1),
            lambda c:c['media'].update(chunked_prefill=False),
            lambda c:c['media'].update(cache_identity='token_placeholders'),
            lambda c:c['media'].update(cache_layout='target_text_only'),
            lambda c:c['media'].update(cache_boundaries='partial_blocks'),
            lambda c:c.update(chat='invalid'),
            lambda c:c['responses'].update(prompt_cache=1),
            lambda c:c['chat']['prompt_cache'].update(media_fingerprint_pending=True),
            lambda c:c['responses']['prompt_cache'].pop('media_fingerprint_pending'),
            lambda c:c['chat']['prompt_cache'].update(media_fingerprint_pending=0)):
            bad = copy.deepcopy(caps); change(bad)
            with self.assertRaises(ValueError):
                profile_migration.semantic_capabilities(bad)

    def test_media_extension_requires_explicit_capacity_and_feature_decision(self):
        registry,evidence=self.extension();before=copy.deepcopy(evidence)
        for kwargs in ({},dict(allow_capacity_change=True),dict(allow_capability_extension=True),
                       dict(allow_capacity_change=True,allow_capability_extension=True,automatic=True)):
            with self.assertRaises(ValueError):self.base.extended_permit(registry,evidence,**kwargs)
        grant=self.base.extended_permit(registry,evidence,allow_capacity_change=True,
                                       allow_capability_extension=True)
        self.assertEqual(grant['format'],profile_migration.EXTENSION_FORMAT)
        self.assertFalse(grant['automatic']);self.assertEqual(evidence,before)
        self.assertNotEqual(grant['profiles'],grant['target_profiles'])
        with self.assertRaises(ValueError):
            profile_migration.permit(registry,self.base.old,evidence,self.base.first,
                self.base.config,self.base.config,allow_capacity_change=True,
                allow_capability_extension=True)

    def test_automatic_same_media_identity_keeps_capacity_and_rejects_device_limit_loss(self):
        registry,evidence=self.extension()
        grant=profile_migration.permit(registry,registry,evidence,evidence,self.base.config,
                                      self.base.config,automatic=True)
        self.assertTrue(grant['automatic'])
        lower=copy.deepcopy(registry)
        records={name:copy.deepcopy(row['record']) for name,row in evidence.items()}
        for record in records.values():record['capabilities']['media']['attention_shared_bytes']=32768
        changed=previous.bind_records(lower,records)
        for automatic in (False,True):
            with self.assertRaises(ValueError):
                profile_migration.permit(registry,lower,evidence,changed,self.base.config,
                    self.base.config,automatic=automatic,allow_capability_extension=True)

    def test_media_declaration_normalization_keeps_original_and_rejects_inconsistent_observation(self):
        _,evidence=self.extension()
        caps=evidence['primary']['record']['capabilities'];original=copy.deepcopy(caps)
        normalized=profile_migration.semantic_capabilities(caps)
        self.assertEqual(caps,original)
        self.assertEqual(normalized['runtime']['media_enabled'],dict(enabled=True))
        for mutate in [lambda c:c['media'].update(attention_shared_bytes=None),
            lambda c:c['media'].update(attention_shared_bytes=True),
            lambda c:c['runtime'].update(media_enabled=1),
            lambda c:c['file_inputs']['native_video'].update(model_media_enabled=False),
            lambda c:c['file_inputs']['native_video'].update(requires_mtp_disabled='false')]:
            bad=copy.deepcopy(caps);mutate(bad)
            with self.assertRaises(ValueError):profile_migration.semantic_capabilities(bad)
        disabled=copy.deepcopy(caps)
        disabled['runtime']['media_enabled']=False
        disabled['media']['attention_shared_bytes']=None
        disabled['file_inputs']['native_video']['model_media_enabled']=False
        old=profile_migration.semantic_capabilities(disabled)
        self.assertTrue(profile_migration.extends(old,normalized))
        self.assertFalse(profile_migration.extends(normalized,old))


if __name__=='__main__':unittest.main()
