"""Negative checks for the owned worker fault and honest failure stream."""
import copy
import unittest

from tools.serve_responses import reconciliation_acceptance as stage


class Driver(unittest.TestCase):
    def test_worker_selection_requires_exact_child_and_known_identity(self):
        own = dict(ppid=10, start_ticks=42, executable='/owned/q4t', argv=['q4t', '--q4t-object-worker'])
        foreign = dict(own, ppid=11)
        self.assertEqual(stage.own_object_worker({20: own, 21: foreign}, 10), (20, own))
        for records in ({21: foreign}, {20: dict(own, metadata_missing=True)},
                        {20: dict(own, argv=['q4t', '--q4t-grammar-worker'])},
                        {20: own, 22: own}):
            with self.assertRaises(RuntimeError):
                stage.own_object_worker(records, 10)

    def test_fault_stream_requires_contiguous_partial_and_one_unknown_terminal(self):
        events = [dict(type='response.created', sequence_number=0),
                  dict(type='response.output_text.delta', sequence_number=1),
                  dict(type='error', sequence_number=2, code='object_commit_unknown')]
        stage.check_fault(events)
        bad = copy.deepcopy(events)
        bad[1]['sequence_number'] = 2
        completed = copy.deepcopy(events)
        completed[-1]['type'] = 'response.completed'
        for value in (bad, completed, events[1:], events+events[-1:], events[:-1]):
            with self.assertRaises(RuntimeError):
                stage.check_fault(value)


if __name__ == '__main__':
    unittest.main()
