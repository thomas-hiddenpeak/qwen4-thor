"""Read-only contracts against bounded real helper and failure captures."""
import json
from pathlib import Path
import unittest
from lifecycle import audit, records, signed

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-pwrite-stack-20261029'


class StackContract(unittest.TestCase):
    def check_finalization(self, directory):
        row = json.loads((directory/'exit.json').read_text())
        events = list(records(directory/'events.bin'))
        self.assertEqual(row['events'], row['counters'][1])
        self.assertEqual(row['events'], len(events))
        self.assertEqual(row['persisted_events'], len(events))
        self.assertEqual(row['pending_bytes'], 0)
        self.assertEqual(row['partial_record_bytes'], 0)
        self.assertEqual(row['finalization_errors'], [])
        for name in ('stacks.json', 'programs.json', 'collector-io.json',
                     'stack-errors.json', 'finalization.json'):
            self.assertTrue(json.loads((directory/name).read_text()))
        return row, events

    def test_real_eexist_failure_keeps_errno_events_and_original_stack(self):
        directory = BASE/'collision/fixture-01'
        row, events = self.check_finalization(directory)
        self.assertEqual(row['status'], 'FAILED')
        self.assertEqual(row['stack_diagnostic']['first_errno'], 17)
        self.assertEqual(row['counters'][4], 1)
        negative = [signed(e['d'][2]) for e in events
                    if e['kind'] == 3 and e['d'][1] and signed(e['d'][2]) < 0]
        self.assertEqual(negative, [-17])
        self.assertEqual(len(json.loads((directory/'stacks.json').read_text())), 1)

    def test_recovered_collision_closes_real_io(self):
        directory = BASE/'recovered/fixture-02'
        row, _ = self.check_finalization(directory)
        result = audit(directory)
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['calls']), 6)
        self.assertEqual(sum(c['written'] for c in result['calls']), 17047552)
        self.assertEqual(row['stack_diagnostic']['recovered'], 1)
        stacks = json.loads((directory/'stacks.json').read_text())
        self.assertNotEqual(stacks['0'], stacks['4096'])

    def test_repeated_collision_ids_survive_abnormal_exit(self):
        directory = BASE/'stack-probe'
        row, events = self.check_finalization(directory)
        self.assertEqual(row['status'], 'FAILED')
        self.assertIn('pending=4352', row['error'])
        self.assertEqual(row['stack_diagnostic']['recovered'], 16)
        stacks = json.loads((directory/'stacks.json').read_text())
        ids = [signed(e['d'][0]) for e in events if e['kind'] == 50]
        self.assertTrue(all(str(i) in stacks for i in ids))
        self.assertEqual(set(i for i in ids if i >= 4096), set(range(4096,4112)))
        self.assertTrue(all(stacks[str(i)] == stacks['4096'] for i in range(4096,4112)))
        self.assertNotEqual(stacks['0'], stacks['4096'])


if __name__ == '__main__': unittest.main()
