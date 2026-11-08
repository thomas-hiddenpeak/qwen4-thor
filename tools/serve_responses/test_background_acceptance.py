"""Failure-sensitive checks of background business acceptance, without GPU."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, call

from tools.serve_responses import background_acceptance as stage


class Driver(unittest.TestCase):
    def test_phase_alarm_reserves_cleanup_and_disarms_on_failure(self):
        runner = Mock()
        runner.run.side_effect = RuntimeError('owned failure')
        with patch.object(stage.sys, 'argv', ['driver', 'run', '--plan', 'unused']), \
             patch.object(stage, 'read', return_value={}), \
             patch.object(stage, 'Stage', return_value=runner), \
             patch.object(stage.base.lifecycle, 'child_subreaper', return_value=nullcontext()), \
             patch.object(stage.signal, 'signal', return_value='previous') as handler, \
             patch.object(stage.signal, 'setitimer') as timer:
            with self.assertRaisesRegex(RuntimeError, 'owned failure'):
                stage.main()
        self.assertEqual(timer.call_args_list, [call(stage.signal.ITIMER_REAL, 860),
                                               call(stage.signal.ITIMER_REAL, 0)])
        self.assertEqual(handler.call_args, call(stage.signal.SIGALRM, 'previous'))

    def response(self):
        path = stage.ROOT/'docs/evidence/serve-responses-declarations-20261109/declarations-sdk-01/work/json/wire.jsonl'
        for line in path.read_text().splitlines():
            row = json.loads(line)
            value = json.loads(row['body'])
            if value.get('status') == 'completed':
                return value
        self.fail('actual fixture completed response absent')

    def test_requests_fit_bound_and_keep_real_tool_history_dynamic(self):
        rows = stage.requests()
        self.assertEqual(len(rows), 8)
        self.assertEqual(stage.LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertTrue(rows['cancel_running']['stream'])
        self.assertFalse(rows['plain']['store'])
        self.assertNotIn('previous_response_id', rows['follow'])
        for name in ('cancel_running', 'crash_running'):
            self.assertEqual(rows[name]['text']['format']['schema']['properties']['values']['minItems'], 512)
            self.assertEqual(rows[name]['max_output_tokens'], 1536)
        self.assertEqual(rows['chat']['max_completion_tokens'], 64)

    def test_final_response_rejects_missing_wire_fields_and_false_usage(self):
        response = self.response()
        stage.check_response(response, 'completed')
        for field in ('tool_choice',):
            bad = copy.deepcopy(response)
            del bad[field]
            with self.assertRaises(RuntimeError):
                stage.check_response(bad)
        for value in (None, -1, True, response['usage']['input_tokens']+1):
            bad = copy.deepcopy(response)
            bad['usage']['input_tokens_details']['cache_write_tokens'] = value
            with self.assertRaises(RuntimeError):
                stage.check_response(bad)
        with self.assertRaises(RuntimeError):
            stage.check_response(response, 'queued')

    def test_unexpected_terminal_cannot_pass_queued_or_partial_checkpoint(self):
        root = stage.ROOT/'.q4t-work/serve-responses-20261108/driver-cpu-work'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            runner = object.__new__(stage.Stage)
            runner.out = Path(folder)
            runner.same_model = Mock()
            value = self.response()
            runner.http = Mock(return_value=(200, json.dumps(value).encode()))
            with self.assertRaisesRegex(RuntimeError, 'unexpected early terminal'):
                runner.wait_response('must-be-queued', value['id'], lambda v: v['status'] == 'queued')
            self.assertEqual(runner.http.call_count, 1)
            self.assertEqual(json.loads((runner.out/'must-be-queued.poll.json').read_text()), [value])


if __name__ == '__main__':
    unittest.main()
