"""Acceptance itself must reject budget, framing and accounting failures."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from tools.serve_observation_capacity import stage, verify
from tools.serve_ops import fixtures


def accepted_counts():
    before = fixtures.metrics(version=4)
    after = copy.deepcopy(before)
    after.update(q4t_requests_total=4, q4t_requests_success_total=3,
        q4t_requests_aborted_total=1, q4t_structured_workers_started_total=2,
        q4t_structured_workers_reaped_total=2)
    for kind, count in (('business', 1), ('observation', 4)):
        after[f'q4t_connection_rejections_total{{kind="{kind}"}}'] = count
    for reason, count in (('connection_capacity', 5), ('body_eof', 127), ('unexpected_body', 4)):
        after[f'q4t_http_receive_failures_total{{reason="{reason}"}}'] = count
    return before, after


class Driver(unittest.TestCase):
    def run_state(self):
        run = verify.Run.__new__(verify.Run)
        run.deadline = 100
        run.hold_started = run.prom_started = None
        run.counts = dict(stage.PLANNED)
        run.control_count = 0
        return run

    def test_budget_refuses_before_increment_and_original_deadline(self):
        for kind in stage.PLANNED:
            run = self.run_state();run.counts[kind] = stage.LIMITS[kind]
            with patch.object(verify.time, 'time', return_value=1), self.assertRaises(RuntimeError):
                run.budget(kind)
            self.assertEqual(run.counts[kind], stage.LIMITS[kind])
        run = self.run_state()
        with patch.object(verify.time, 'time', return_value=100), self.assertRaises(RuntimeError):
            run.budget('generations')
        self.assertEqual(run.counts['generations'], 4)

    def test_observation_reservations_are_not_unbounded_polling(self):
        run = self.run_state();run.control_count = 255
        with patch.object(verify.time, 'time', return_value=1), self.assertRaises(RuntimeError):
            run.control(5)
        self.assertEqual(run.control_count, 255)
        for key, elapsed in (('hold_started', 25), ('prom_started', 30)):
            run = self.run_state();setattr(run, key, 0)
            with patch.object(verify.time, 'time', return_value=1), \
                 patch.object(verify.time, 'monotonic', return_value=elapsed), self.assertRaises(RuntimeError):
                run.budget()

    def test_accounting_accepts_exact_attempts_and_separate_owner_contract(self):
        verify.check_accounting(*accepted_counts())

    def test_lost_refusals_false_success_and_owner_leaks_are_not_accepted(self):
        for key in ('q4t_requests_success_total', 'q4t_requests_aborted_total',
                    'q4t_structured_workers_reaped_total', 'q4t_business_connections',
                    'q4t_observation_connections', 'q4t_request_body_bytes',
                    'q4t_connection_rejections_total{kind="observation"}',
                    'q4t_http_receive_failures_total{reason="body_eof"}'):
            before, after = accepted_counts();after[key] += 1
            with self.assertRaises(RuntimeError):verify.check_accounting(before, after)

    def test_error_wire_requires_complete_body_status_and_code(self):
        validator = Draft202012Validator(json.loads((stage.ROOT/'docs/serve-transport-v1.schema.json').read_text()))
        body = b'{"error":{"message":"capacity","type":"server_error","code":"observation_capacity_exhausted","param":null}}'
        raw = b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
        verify.checked_error(raw, 503, 'observation_capacity_exhausted', validator)
        for broken, status, code in ((raw[:-1], 503, 'observation_capacity_exhausted'),
                (raw, 200, 'observation_capacity_exhausted'), (raw, 503, 'capacity_exhausted')):
            with self.assertRaises(RuntimeError):verify.checked_error(broken, status, code, validator)

    def test_frozen_rollback_does_not_depend_on_working_source_hashes(self):
        plan = dict(root=str(stage.ROOT), limits=stage.LIMITS,
                    previous_binary_sha256=stage.DEFAULT_SHA, old_hashes={'q4t':stage.DEFAULT_SHA},
                    binary={'path':'sealed-candidate'})
        with patch.object(stage.h, 'verify_seal'), patch.object(stage.h, 'read', return_value=plan), \
             patch.object(stage.h, 'check_identity'), patch.object(stage.h, 'verify_pair'), \
             patch.object(stage.h, 'sha', return_value=stage.LEGACY_RULE_SHA), \
             patch.object(stage.a, 'verify_hashes', side_effect=AssertionError('working tree required')):
            self.assertEqual(stage.rollback_plan(), plan)


if __name__ == '__main__':unittest.main()
