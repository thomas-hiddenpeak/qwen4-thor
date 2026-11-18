"""Targeted recovery verifier tests; synthetic CPU states never qualify models."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from tools.native_asr import recovery_profiles as checked
from tools.serve_recovery import ledger, profile_contract, profile_memory
from tools.serve_recovery import profile_policy as policy
from tools.serve_recovery.test_profile_policy import registry


class RecoveryProfiles(unittest.TestCase):
    def setUp(self):
        self.registry = registry()
        self.boot = str(uuid.uuid4())
        self.before = policy.initial(self.registry, self.stamp(0))
        self.sample = dict(total_bytes=128*1024**3,
            available_bytes=self.registry['qualifications']['light']['startup_required_bytes'])
        prepared = policy.prepare(self.before, 'light', 'cold_memory_capacity',
            str(uuid.uuid4()), 0, self.stamp(1))
        self.after = policy.finish(prepared, 2, prepared['profiles']['light'], self.stamp(2))
        self.row = dict(payload='fallback', monotonic_ns=ledger.SECOND,
            registry_sha256=profile_contract.digest(self.registry),
            state=copy.deepcopy(self.before), observation=copy.deepcopy(self.sample),
            result=profile_memory.cold(self.registry, self.before, self.sample))

    def stamp(self, seconds):
        return ledger.Stamp(self.boot, seconds*ledger.SECOND)

    def check(self, after=None, rows=None):
        return checked.continuity(self.before, self.after if after is None else after,
            self.registry, [self.row] if rows is None else rows, 2)

    def test_primary_preserved_or_exact_observed_downgrade_charges_one_switch(self):
        same = policy.resume(self.before, self.stamp(10))
        self.assertEqual(self.check(same, []), dict(kind='unchanged_primary'))
        result = self.check()
        self.assertEqual(result['kind'], 'observed_cold_memory_downgrade')
        self.assertTrue(result['history_preserved'] and result['cooldown_preserved'])
        self.assertEqual(result['available_bytes'], result['light_required_bytes'])
        self.assertLess(result['available_bytes'], result['primary_required_bytes'])
        self.assertEqual(len(self.after['history']), 1)

    def test_missing_unknown_false_or_stale_observation_cannot_excuse_downgrade(self):
        with self.assertRaises(ValueError): self.check(rows=[])
        for mutate in [lambda x:x.update(payload='primary'),
            lambda x:x.update(registry_sha256='0'*64),
            lambda x:x.update(observation={}),
            lambda x:x['observation'].update(available_bytes=True),
            lambda x:x['observation'].update(available_bytes=
                self.registry['qualifications']['primary']['startup_required_bytes']),
            lambda x:x['observation'].update(available_bytes=0),
            lambda x:x.update(monotonic_ns=2*ledger.SECOND),
            lambda x:x['result'].update(ready=True)]:
            row = copy.deepcopy(self.row);mutate(row)
            with self.assertRaises(ValueError): self.check(rows=[row])

    def test_history_budget_contract_or_generation_changes_are_rejected(self):
        for mutate in [lambda x:x['history'].clear(),
            lambda x:x.update(not_before_ns=0),
            lambda x:x.update(completed_generation=1),
            lambda x:x['configuration'].update(cooldown_seconds=60),
            lambda x:x.update(registry_sha256='0'*64),
            lambda x:x['history'][0].update(reason='confirmed_memory_fault'),
            lambda x:x['history'][0].update(source_generation=2),
            lambda x:x['history'][0].update(target_sha256='0'*64),
            lambda x:x.update(light_since_ns=0),
            lambda x:x['clock'].update(boot_id=str(uuid.uuid4()))]:
            after = copy.deepcopy(self.after);mutate(after)
            with self.assertRaises(ValueError): self.check(after)
        same = policy.resume(self.before, self.stamp(10))
        same['not_before_ns'] = ledger.SECOND
        with self.assertRaises(ValueError): self.check(same, [])

    def test_observer_preserves_production_arguments_result_and_disabled_path(self):
        root = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
            Path(__file__).resolve().parents[2]/'.q4t-work/native-asr-recovery-profile-tests'))
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            out = Path(directory)
            original = profile_memory.cold
            checked.observe({}, 'fallback', out)
            self.assertIs(profile_memory.cold, original)
            result = original(self.registry, self.before, self.sample)
            returned = dict(result)
            sentinel = mock.Mock(return_value=returned)
            before = copy.deepcopy((self.registry, self.before, self.sample))
            with mock.patch.object(profile_memory, 'cold', sentinel), \
                 mock.patch.object(checked.time, 'monotonic_ns', return_value=ledger.SECOND):
                checked.observe(dict(observe_cold_profiles=True), 'fallback', out)
                actual = profile_memory.cold(self.registry, self.before, self.sample)
                self.assertIs(actual, returned)
                args = sentinel.call_args.args
                self.assertIs(args[0], self.registry)
                self.assertIs(args[1], self.before)
                self.assertIs(args[2], self.sample)
            self.assertEqual((self.registry, self.before, self.sample), before)
            rows = [json.loads(line) for line in (out/'cold-profiles-fallback.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.check(rows=rows)['kind'], 'observed_cold_memory_downgrade')
            self.assertIs(profile_memory.cold, original)


if __name__ == '__main__':
    unittest.main()
