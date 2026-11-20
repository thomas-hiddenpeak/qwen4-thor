"""Accounting-only reuse must not conceal other runtime or evidence changes."""
import copy
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_record
from tools.serve_recovery import profile_speech_reuse as reuse


class CancellationSource(unittest.TestCase):
    def setUp(self):
        self.old = dict(format='q4t-profile-business-v19', profile='primary',
            identity=dict(binary_sha256='a'*64, protocol_sha256='b'*64,
                          model_assets=dict(weights='fixed')),
            capacity=dict(max_sequences=1),
            capabilities=dict(protocol_source_sha256='b'*64, audio=True),
            configuration=dict(protocol_sha256='b'*64, mtp_enabled=True),
            startup_memory=dict(used=10), environment=dict(status='PASS'),
            business=dict(realtime_speech=dict(audio='original'),
                          tool_reply=dict(version='rev366')),
            reused_business=dict(format=reuse.FORMAT, original_failure='retained'))
        source = {k: ledger.digest(self.old[k]) for k in
            ('identity', 'capacity', 'capabilities', 'configuration',
             'startup_memory', 'environment')}
        source.update(record_sha256='e'*64,
            prior_reuse=ledger.digest(self.old['reused_business']),
            business={k: ledger.digest(v) for k,v in self.old['business'].items()})
        review = dict(reuse.CANCEL_REVIEW, previous_binary='a'*64,
            previous_protocol='b'*64, candidate_binary='c'*64,
            candidate_protocol='d'*64)
        for key,value in [('CANCEL_REVIEW',review),
                          ('CANCEL_SOURCES',dict(primary=source))]:
            patch = mock.patch.object(reuse,key,value)
            patch.start(); self.addCleanup(patch.stop)

    def reject(self, mutate):
        value = reuse.attach_cancellation(self.old)
        mutate(value)
        with self.assertRaises((ValueError, KeyError)):
            reuse.validate(value)

    def test_exact_sources_keep_history_and_separate_fresh_repair(self):
        original = copy.deepcopy(self.old)
        value = reuse.attach_cancellation(self.old)
        reuse.validate(value)
        self.assertEqual(self.old,original)
        self.assertEqual(value['business'],original['business'])
        self.assertEqual(value['environment'],original['environment'])
        self.assertEqual(value['reused_business']['repair'],reuse.CANCEL_REPAIR)
        self.assertNotIn('record',value['reused_business'])

    def test_relabelled_failed_or_modified_repair_never_qualifies(self):
        for mutate in [lambda v:v['reused_business']['repair'].update(status='FAIL'),
            lambda v:v['reused_business']['repair']['business'].update(request_errors=1),
            lambda v:v['reused_business']['repair']['business'].update(model_restarts=1),
            lambda v:v['reused_business']['repair']['evidence'].update(**{'run/business.json':'0'*64}),
            lambda v:v['reused_business']['repair']['environment'].update(env_abort=True)]:
            self.reject(mutate)

    def test_different_binary_protocol_or_backend_cannot_reuse(self):
        for mutate in [lambda v:v['identity'].update(binary_sha256='0'*64),
            lambda v:v['identity'].update(protocol_sha256='0'*64),
            lambda v:v['identity']['model_assets'].update(weights='other')]:
            self.reject(mutate)

    def test_capacity_or_observed_configuration_cannot_drift(self):
        for mutate in [lambda v:v['capacity'].update(max_sequences=2),
            lambda v:v['configuration'].update(mtp_enabled=False),
            lambda v:v['configuration'].update(protocol_sha256='0'*64),
            lambda v:v['capabilities'].update(audio=False),
            lambda v:v['capabilities'].update(protocol_source_sha256='0'*64)]:
            self.reject(mutate)

    def test_all_original_business_is_exact_and_cannot_be_added_or_removed(self):
        for mutate in [lambda v:v['business']['tool_reply'].update(version='wrong'),
            lambda v:v['business']['realtime_speech'].update(audio='changed'),
            lambda v:v['business'].pop('tool_reply'),
            lambda v:v['business'].update(new_claim=True),
            lambda v:v['reused_business'].update(keys=[])]:
            self.reject(mutate)

    def test_old_environment_memory_and_provenance_are_not_new_measurements(self):
        for mutate in [lambda v:v['environment'].update(status='FAIL'),
            lambda v:v['startup_memory'].update(used=9),
            lambda v:v['reused_business'].update(prior_reuse_sha256='0'*64),
            lambda v:v['reused_business']['source'].update(record_sha256='0'*64),
            lambda v:v['reused_business']['review'].update(runtime_diff='0'*64)]:
            self.reject(mutate)

    def test_attachment_cannot_hide_unknown_original_identity(self):
        for field,key,new in [('identity','binary_sha256','0'*64),
            ('capabilities','audio',False), ('configuration','mtp_enabled',False),
            ('reused_business','original_failure','erased')]:
            value = copy.deepcopy(self.old); value[field][key] = new
            with self.assertRaises(ValueError): reuse.attach_cancellation(value)

    def test_evidence_bound_remains_explicit_and_finite(self):
        value = reuse.attach_cancellation(self.old)
        profile_record.shape(value,8<<20)
        for changed,size in [(value,(8<<20)+1),
            (dict(value,format='q4t-profile-business-v18'),3<<20),
            (dict(value,reused_business=dict(format='unknown')),3<<20)]:
            with self.assertRaises(ValueError): profile_record.shape(changed,size)


if __name__ == '__main__': unittest.main()
