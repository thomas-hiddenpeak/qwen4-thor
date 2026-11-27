"""Native short and day reports must bind successful polls to their original replies."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_soak import evidence, native_observations as observations


class ObservationTest(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.output = Path(area.name)
        for name in ('execution', 'capture'):
            (self.output/name).mkdir()
        self.lease = dict(compute_backend='nvml', status='COMPLETED', child_returncode=0,
                          lease_inherited_by_child=True)
        self.initial = dict(model_pid=35, model_start_ticks=71)
        self.events = [
            dict(event='lease_acquired', monotonic=1),
            dict(event='preflight', monotonic=2, conflicts=[]),
            dict(event='monitor', monotonic=5, conflicts=[]),
            dict(event='lease_released', monotonic=6, status='COMPLETED'),
        ]
        self.queries = [self.reply(1.5, []), self.reply(3.5, [35])]
        self.status = [self.initial | dict(monotonic=t, business_ready=True) for t in (3, 4)]

    def reply(self, when, pids):
        final = dict(event='result', monotonic=when, request_id=str(when), status='OK',
                     pids=pids, device_count=1)
        return dict(request_id=str(when), status='OK', pids=pids, device_count=1,
                    returncode=0, stdout=json.dumps(final)+'\n')

    def check(self):
        for path, rows in [('capture/nvml-queries.jsonl', self.queries),
                            ('capture/events.jsonl', self.events), ('execution/status.jsonl', self.status)]:
            (self.output/path).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return observations.verify(self.output, self.lease, self.initial)

    def test_monitor_can_requery_after_an_owned_process_exits(self):
        self.events[2]['retired_owned_contexts'] = [{'pid': 18}]
        self.queries.append(self.reply(4.3, []))
        self.assertEqual(self.check(), dict(guard_scans=2, nvml_queries=3, additional_queries=1,
            model_context_queries=1, model_context_queries_during_ready=1))
        self.queries.pop()
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()

    def test_capture_requires_a_query_for_each_scan(self):
        self.queries.pop(0)
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()

    def test_missing_model_and_nonready_gap(self):
        self.status.insert(1, self.initial | dict(monotonic=3.2, business_ready=False))
        with self.assertRaisesRegex(evidence.EvidenceAbort, 'observed ready model'):
            self.check()
        self.status.pop(1)
        self.queries[-1] = self.reply(3.5, [88])
        with self.assertRaisesRegex(evidence.EvidenceAbort, 'observed ready model'):
            self.check()

    def test_reused_pid_or_changed_ready_identity(self):
        self.status[-1]['model_start_ticks'] += 1
        with self.assertRaisesRegex(evidence.EvidenceAbort, 'another model'):
            self.check()

    def test_body_bound_to_query_summary_and_request(self):
        self.queries[-1]['pids'] = []
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()
        self.queries[-1] = self.reply(3.5, [35]) | dict(request_id='unrelated')
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()

    def test_reject_duplicate_final_and_regressing_reply_clock(self):
        self.queries[-1]['stdout'] *= 2
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()
        self.queries[-1] = self.reply(1.25, [35])
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()

    def test_unfinished_or_trailing_queries(self):
        self.events.pop()
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()
        self.events.append(dict(event='lease_released', monotonic=6, status='COMPLETED'))
        self.queries.append(self.reply(5.25, []))
        with self.assertRaises(evidence.EvidenceAbort):
            self.check()

    def test_truncated_last_record_is_rejected(self):
        path = self.output/'broken.jsonl'
        path.write_text('{"status":"OK"}')
        with self.assertRaisesRegex(evidence.EvidenceAbort, 'incomplete'):
            list(observations.records(path))
