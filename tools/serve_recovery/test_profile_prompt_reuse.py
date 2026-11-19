"""CPU source-lineage counterexamples, never an authorization to run a model."""
import copy
import hashlib
from unittest import mock
import unittest

from tools.serve_recovery import ledger, profile_contract, profile_evidence
from tools.serve_recovery import profile_prompt_reuse as reuse
from tools.serve_recovery import test_profile_conversation as previous
from tools.serve_recovery import test_profile_prompt as current
from tools.serve_recovery import test_profile_migration as binding


def legacy():
    return dict(definition=reuse.legacy_definition(),
        registered=dict(id='prompt_cpu', object='prompt', revision=1, default_version='1'),
        request=reuse.legacy_request('prompt_cpu'), response=dict(object='response', status='completed',
            error=None, incomplete_details=None, prompt=dict(id='prompt_cpu', version='1'),
            output=[dict(type='message', content=[dict(type='output_text', text='42')])],
            usage=dict(input_tokens=50, output_tokens=3, total_tokens=53)))


class SourceReuse(unittest.TestCase):
    stamp = previous.ConversationMigration.stamp

    def setUp(self):
        previous.ConversationMigration.setUp(self)
        _, records = previous.ConversationMigration.pair(self)
        self.source_registry = copy.deepcopy(self.new)
        self.original = copy.deepcopy(records['primary']['record'])
        self.original_light = copy.deepcopy(records['light']['record'])
        # Match the real semantic receipt serialization, rather than the older
        # synthetic migration helper's intentionally arbitrary digest format.
        digest = hashlib.sha256(ledger.canonical(self.original) + b'\n').hexdigest()
        self.source_registry['qualifications']['primary'] = profile_contract.qualification(
            'primary', self.original['capacity'], self.original['identity'], self.original['capabilities'],
            self.original['configuration'], self.original['startup_memory'], digest)
        self.value = copy.deepcopy(self.original)
        self.value['format'] = profile_evidence.PROMPT_FORMAT
        self.value['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        self.value['capabilities']['protocol_source_sha256'] = 'f'*64
        self.value['configuration']['protocol_sha256'] = 'f'*64
        self.value['capabilities']['realtime']['conversation'] = current.declaration()
        self.value['business']['prompt'] = current.record()
        review = dict(reuse.REVIEW, previous_binary=self.original['identity']['binary_sha256'],
            candidate_binary='e'*64, candidate_protocol='f'*64,
            previous_registry=ledger.digest(self.source_registry))
        patch = mock.patch.object(reuse, 'REVIEW', review)
        patch.start(); self.addCleanup(patch.stop)

    def attached(self):
        return reuse.attach(self.value, self.source_registry, self.original, legacy())

    def test_explicit_original_source_is_preserved_and_new_record_validates(self):
        snapshot = copy.deepcopy((self.original, self.source_registry, self.value))
        value = self.attached()
        self.assertEqual(value['reused_business']['record'], self.original)
        self.assertEqual(value['reused_business']['keys'], ['embedding'])
        self.assertEqual((self.original, self.source_registry, self.value), snapshot)
        target = copy.deepcopy(self.source_registry)
        target['identity'] = copy.deepcopy(value['identity'])
        records = {'primary': value, 'light': copy.deepcopy(self.original_light)}
        records['light']['format'] = profile_evidence.PROMPT_FORMAT
        records['light']['capabilities']['realtime']['conversation'] = current.declaration()
        records['light']['business']['prompt'] = current.record()
        # Only primary is evaluated in this CPU test; a real pair must have
        # its own independently bound original light record and actual work.
        bound = binding.bind_records(target, records)
        profile_evidence.validate(value, target, 'primary', bound['primary']['sha256'])

    def test_unknown_origin_or_recursive_transfer_is_rejected(self):
        value = self.attached()
        for mutate in [lambda v: v['reused_business']['record']['business']['schema'].update(id='changed'),
                       lambda v: v['reused_business']['registry']['identity'].update(binary_sha256='0'*64),
                       lambda v: v['reused_business']['record'].update(reused_business={}),
                       lambda v: v['reused_business']['review'].update(source_review='0'*64)]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.validate(changed)

    def test_other_build_asset_capacity_or_capability_cannot_reuse(self):
        value = self.attached()
        for mutate in [lambda v: v['identity'].update(binary_sha256='1'*64),
                       lambda v: v['identity'].update(protocol_sha256='1'*64),
                       lambda v: v['identity'].update(native_embedding={}),
                       lambda v: v['capacity'].update(max_tokens=1),
                       lambda v: v['capabilities']['chat'].update(invented=True)]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.validate(changed)

    def test_fresh_scope_cannot_be_replaced_by_changed_old_values(self):
        value = self.attached()
        for mutate in [lambda v: v['business'].update(embedding={}),
                       lambda v: v['business'].pop('conversation'),
                       lambda v: v['reused_business']['keys'].append('conversation'),
                       lambda v: v['business'].update(unknown={}),
                       lambda v: v.update(format=profile_evidence.CONVERSATION_FORMAT)]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.validate(changed)

    def test_legacy_responses_requires_actual_source_version_answer_and_usage(self):
        value = legacy(); reuse.legacy_business(value)
        for mutate in [lambda v: v['definition'].update(api='realtime'),
                       lambda v: v['response']['prompt'].update(version='2'),
                       lambda v: v['request']['prompt'].update(id='foreign'),
                       lambda v: v['response']['output'][0]['content'][0].update(text='43'),
                       lambda v: v['response']['usage'].update(total_tokens=0),
                       lambda v: v['response'].update(status='failed')]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.legacy_business(changed)

    def test_unrelated_legacy_record_still_uses_original_strict_contract(self):
        digest = self.source_registry['qualifications']['primary']['evidence_sha256']
        profile_evidence.validate(self.original, self.source_registry, 'primary', digest)
        changed = copy.deepcopy(self.original)
        changed['business']['conversation']['session']['responses'][1]['status'] = 'failed'
        with self.assertRaises(ValueError):
            profile_evidence.validate(changed, self.source_registry, 'primary', digest)


if __name__ == '__main__': unittest.main()
