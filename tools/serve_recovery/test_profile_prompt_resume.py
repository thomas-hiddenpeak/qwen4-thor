"""Partial-stage provenance cannot erase a failed component or expand work."""
import copy
import unittest
from unittest import mock

from tools.serve_recovery import ledger
from tools.serve_recovery import profile_prompt_reuse as reuse
from tools.serve_recovery import profile_prompt_targeted as driver


class Resume(unittest.TestCase):
    def setUp(self):
        self.value = dict(profile='primary', identity=dict(binary_sha256='a' * 64),
            capacity=dict(max_len=8192), capabilities=dict(prompt=True),
            business={key: dict(observation=key) for key in reuse.FRESH})
        self.proof = dict(format='q4t-prompt-primary-text-continuation-v1',
            original_failure_preserved=True, original_result=dict(status='FAILED'),
            identity=copy.deepcopy(self.value['identity']), capacity=copy.deepcopy(self.value['capacity']),
            capabilities=copy.deepcopy(self.value['capabilities']),
            business={k: copy.deepcopy(v) for k, v in self.value['business'].items() if k != 'prompt'})
        patch = mock.patch.object(reuse, 'PRIMARY_PARTIAL', ledger.digest(self.proof))
        patch.start(); self.addCleanup(patch.stop)

    def test_only_exact_completed_text_is_reused_and_failure_remains(self):
        before = copy.deepcopy((self.value, self.proof))
        reuse.continued_business(self.value, self.proof)
        self.assertEqual((self.value, self.proof), before)
        self.assertEqual(self.proof['original_result']['status'], 'FAILED')
        self.assertNotIn('prompt', self.proof['business'])

    def test_relabeling_original_failure_or_partial_contents_is_rejected(self):
        for mutate in (lambda p: p['original_result'].update(status='PASS'),
                       lambda p: p.update(original_failure_preserved=False),
                       lambda p: p['business'].update(prompt={}),
                       lambda p: p['business'].pop('conversation'),
                       lambda p: p.update(format='unknown')):
            changed = copy.deepcopy(self.proof); mutate(changed)
            with self.assertRaises(ValueError): reuse.continued_business(self.value, changed)

    def test_partial_observations_cannot_cross_identity_capacity_profile_or_change(self):
        for mutate in (lambda v: v.update(profile='light'),
                       lambda v: v['identity'].update(binary_sha256='b' * 64),
                       lambda v: v['capacity'].update(max_len=4096),
                       lambda v: v['capabilities'].update(prompt=False),
                       lambda v: v['business'].update(schema={})):
            changed = copy.deepcopy(self.value); mutate(changed)
            with self.assertRaises(ValueError): reuse.continued_business(changed, self.proof)

    def test_prompt_observations_must_be_new_and_old_text_does_not_cover_them(self):
        changed = copy.deepcopy(self.value)
        changed['business']['prompt'] = dict(actual_new_response='required_elsewhere')
        reuse.continued_business(changed, self.proof)
        # The outer strict business validator separately checks this component;
        # adding it to the retained-source set must never bypass that validator.
        proof = copy.deepcopy(self.proof); proof['business']['prompt'] = changed['business']['prompt']
        with self.assertRaises(ValueError): reuse.continued_business(changed, proof)

    def test_resume_and_full_counts_are_not_interchangeable(self):
        counts = {k: v for k, v in driver.RESUME_LIMITS.items() if k != 'seconds'}
        counts['controls'] = 30
        result = dict(status=driver.SUCCESS, binary_sha256=reuse.REVIEW['candidate_binary'],
                      seconds=100, counts=counts)
        driver.check_result(result, driver.RESUME_LIMITS)
        with self.assertRaises(RuntimeError): driver.check_result(result)
        for key in ('chats', 'starts', 'prompt_registry_requests', 'websocket_sessions'):
            bad = copy.deepcopy(result); bad['counts'][key] -= 1
            with self.assertRaises(RuntimeError): driver.check_result(bad, driver.RESUME_LIMITS)


if __name__ == '__main__': unittest.main()
