"""CPU-only media migration: explicit extension, capability-preserving recovery."""
import copy
import unittest

from tools.serve_recovery import profile_contract, profile_evidence, profile_migration
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
