"""Synthetic cancellation evidence contracts; never launch a model/server."""
import copy
import hashlib
from http.client import HTTPResponse
import io
import json
import threading
import unittest

import request_cancellation_contract as contract


def message(label, content='example'):
    return {'id': label, 'choices': [{'delta': {'content': content},
                                     'finish_reason': None}]}


def response(label=contract.CONTROL, cancelled=False, complete=True):
    messages = [message(label)]
    if cancelled:
        messages.append({'error': {'message': 'request cancelled'}})
    elif complete:
        messages.extend([
            {'id': label, 'choices': [{'delta': {}, 'finish_reason': 'length'}]},
            {'id': label, 'choices': [], 'usage': {'prompt_tokens': 1024,
                'completion_tokens': 256, 'total_tokens': 1280}},
        ])
    body = ''.join('data: ' + json.dumps(row) + '\n\n' for row in messages)
    if complete:
        body += 'data: [DONE]\n\n'
    return {'status': 200, 'body': body}


def log():
    lines = []
    for label, kind, _ in contract.recovery_plan():
        if kind != 'prefill_cancel':
            steps = 74 if kind in ['control', 'recovery'] else 2
            lines.append(f'[q4t][decode_path] id={label} requested_mtp=1 '
                         f'path=mtp_multi_b1 mtp_steps={steps} fallback=none '
                         'plain_tail_tokens=0')
    for label, reason in contract.INTERRUPTS.items():
        if label == 'same-prefill-cancel':
            lines.append('[q4t] prefill cancelled seq=0 position=8192 total=45056')
        lines.append(f'[q4t] request cancelled id={label} reason={reason}')
    return '\n'.join(lines) + '\n'


class PlanAndFixtureTest(unittest.TestCase):
    def test_exact_nine_request_order_keeps_prefill(self):
        plan = contract.recovery_plan()
        self.assertEqual([kind for _, kind, _ in plan],
                         ['control', 'prefill_cancel', 'recovery', 'explicit',
                          'recovery', 'rst', 'recovery', 'deadline', 'recovery'])
        self.assertEqual(len({label for label, _, _ in plan}), 9)
        self.assertEqual(sum(kind != 'prefill_cancel' for _, kind, _ in plan), 8)
        self.assertEqual(plan[1][2], 'long')
        self.assertEqual(sum(kind == 'recovery' for _, kind, _ in plan), 4)

    def fixture_args(self):
        digest = lambda s: hashlib.sha256(s.encode()).hexdigest()
        return ([{'prompt': 'long-a'}, {'prompt': 'long-b'}],
                [{'id': 'first', 'length': 45056, 'prompt_sha256': digest('long-a')},
                 {'id': 'second', 'length': 45056, 'prompt_sha256': digest('long-b')}],
                [{'prompt': 'short'}] * 3,
                [{'length': 1024, 'prompt_sha256': digest('short'),
                  'outputs': ['historical-output-is-irrelevant']}])

    def test_input_metadata_bound_without_historical_output_oracle(self):
        args = self.fixture_args()
        fixtures, metadata = contract.select_fixtures(*args)
        self.assertEqual(fixtures, {'long': 'long-a', 'decode': 'short'})
        self.assertEqual(metadata['long']['manifest_id'], 'first')
        args[-1][0]['outputs'] = ['completely-different']
        self.assertEqual(contract.select_fixtures(*args), (fixtures, metadata))

    def test_missing_ambiguous_or_changed_fixture_rejected(self):
        for mutation in [lambda a: a[1].clear(),
                         lambda a: a[0].append(a[0][0]),
                         lambda a: a[2].append({'prompt': 'different'}),
                         lambda a: a[3][0].update(prompt_sha256='wrong'),
                         lambda a: a[3].append(a[3][0])]:
            args = self.fixture_args()
            mutation(args)
            with self.assertRaises(ValueError):
                contract.select_fixtures(*args)


class ResponseContractTest(unittest.TestCase):
    def test_fresh_control_and_same_response_recovery(self):
        control = contract.completed_result(response(), contract.CONTROL)
        recovered = contract.completed_result(response('recovered'), 'recovered')
        contract.require_recovery(recovered, control)

    def test_missing_control_and_changed_recovery_rejected(self):
        result = contract.completed_result(response(), contract.CONTROL)
        with self.assertRaises(ValueError):
            contract.require_recovery(result, None)
        for field, value in [('text', 'different'), ('finish', ['stop']),
                             ('usage', [])]:
            changed = copy.deepcopy(result)
            changed[field] = value
            with self.assertRaises(ValueError):
                contract.require_recovery(changed, result)

    def test_normal_completion_cannot_be_cancellation(self):
        with self.assertRaises(ValueError):
            contract.require_cancelled(response(), contract.CONTROL)

    def test_explicit_and_deadline_require_content_and_error(self):
        for label in ['same-decode-cancel', 'same-decode-deadline']:
            contract.require_cancelled(response(label, cancelled=True), label)
            bad = response(label, cancelled=True)
            bad['body'] = bad['body'].replace('example', '')
            with self.assertRaises(ValueError):
                contract.require_cancelled(bad, label)

    def test_error_before_content_is_not_a_decode_interruption(self):
        good = response(cancelled=True)
        events = good['body'].split('\n\n')
        good['body'] = '\n\n'.join([events[1], events[0], *events[2:]])
        with self.assertRaises(ValueError):
            contract.require_cancelled(good, contract.CONTROL)

    def test_prefill_has_its_own_nonstreaming_error(self):
        good = {'status': 409, 'body': json.dumps({
            'error': {'message': 'request cancelled during prefill'}})}
        contract.require_prefill_response(good)
        for bad in [{**good, 'status': 200},
                    {**good, 'body': good['body'].replace(' during prefill', '')},
                    {'status': 409, 'body': response(cancelled=True)['body']}]:
            with self.assertRaises(ValueError):
                contract.require_prefill_response(bad)

    def test_cancellation_rejects_usage_finish_duplicate_error_done(self):
        for event in [{'usage': {}}, {'usage': {'completion_tokens': 1}},
                      {'choices': [{'finish_reason': 'length'}]},
                      {'error': {'message': 'request cancelled'}}]:
            bad = response(cancelled=True)
            bad['body'] = bad['body'].replace('data: [DONE]',
                'data: ' + json.dumps(event) + '\n\ndata: [DONE]')
            with self.assertRaises(ValueError):
                contract.require_cancelled(bad, contract.CONTROL)
        bad = response(cancelled=True)
        bad['body'] += 'data: [DONE]\n'
        with self.assertRaises(ValueError):
            contract.require_cancelled(bad, contract.CONTROL)

    def test_partial_rst_requires_actual_id_and_nonempty_content(self):
        good = response('same-decode-rst', complete=False)
        contract.require_partial(good, 'same-decode-rst')
        for bad in [response('wrong', complete=False),
                    response('same-decode-rst'),
                    {'status': 200, 'body': 'data: {"choices":[]}\n'}]:
            with self.assertRaises(ValueError):
                contract.require_partial(bad, 'same-decode-rst')

    def test_missing_or_duplicate_usage_is_not_success(self):
        good = response()
        usage_line = next(line for line in good['body'].splitlines()
                          if '"usage"' in line)
        for body in [good['body'].replace(usage_line, ''),
                     good['body'].replace(usage_line, usage_line + '\n' + usage_line)]:
            with self.assertRaises(ValueError):
                contract.completed_result({'status': 200, 'body': body}, contract.CONTROL)


class HttpBodyContractTest(unittest.TestCase):
    @staticmethod
    def http_response(raw):
        class MemorySocket:
            def makefile(self, mode):
                return io.BytesIO(raw)
        parsed = HTTPResponse(MemorySocket())
        parsed.begin()
        return parsed

    def chunked_response(self, chunks):
        wire = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
        wire += b''.join(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n'
                         for chunk in chunks)
        return self.http_response(wire + b'0\r\n\r\n')

    def test_chunked_sse_and_utf8_split_across_chunks(self):
        body = response()['body'].replace('example', '内容').encode()
        received, first = bytearray(), threading.Event()
        with self.chunked_response([body[i:i + 7] for i in range(0, len(body), 7)]) as r:
            prefix = contract.read_body(r, received, first)
        self.assertTrue(first.is_set())
        self.assertEqual(bytes(received), body)
        self.assertEqual(prefix, len(body))
        contract.completed_result({'status': 200, 'body': received.decode()},
                                  contract.CONTROL)

    def test_rst_keeps_incomplete_sse_utf8_tail_unparsed(self):
        body = response('same-decode-rst', complete=False)['body'].encode()
        tail = b'data: {"id":"same-decode-rst","content":"' + '内'.encode()[:1]
        received = bytearray()
        with self.chunked_response([body + tail, b'ignored']) as r:
            prefix = contract.read_body(r, received, stop_after_content=True)
        self.assertEqual(bytes(received), body + tail)
        self.assertEqual(bytes(received[prefix:]), tail)
        contract.require_partial({'status': 200,
                                  'body': received[:prefix].decode()}, 'same-decode-rst')

    def test_rst_content_and_completion_in_one_read_is_rejected(self):
        received = bytearray()
        with self.chunked_response([response('same-decode-rst')['body'].encode()]) as r:
            prefix = contract.read_body(r, received, stop_after_content=True)
        with self.assertRaises(ValueError):
            contract.require_partial({'status': 200,
                                      'body': received[:prefix].decode()}, 'same-decode-rst')

    def test_empty_eof_is_not_decode_content(self):
        received, first = bytearray(), threading.Event()
        with self.chunked_response([]) as r:
            contract.read_body(r, received, first, stop_after_content=True)
        self.assertFalse(first.is_set())
        with self.assertRaises(ValueError):
            contract.require_partial({'status': 200, 'body': ''}, 'same-decode-rst')

    def test_truncated_content_length_rejected(self):
        body = response()['body'].encode()
        raw = ('HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n'
               % (len(body) + 10)).encode() + body
        with self.http_response(raw) as r, self.assertRaises(ValueError):
            contract.read_body(r, bytearray())

    def test_response_byte_limit_and_total_deadline_rejected(self):
        with self.chunked_response([b'abcd']) as r, self.assertRaises(ValueError):
            contract.read_body(r, bytearray(), limit=3)
        for expiration in [[True], [False, True]]:
            iterator = iter(expiration)
            received = bytearray()
            with self.chunked_response([b'abcd']) as r, self.assertRaises(ValueError):
                contract.read_body(r, received, expired=lambda: next(iterator))
            self.assertEqual(bytes(received), b'' if len(expiration) == 1 else b'abcd')


class AccountingAndPathTest(unittest.TestCase):
    def test_frozen_nine_four_five_counters(self):
        before = dict(zip(contract.COUNTERS, (10, 2, 8)))
        after = dict(zip(contract.COUNTERS, (19, 6, 13)))
        contract.require_delta(before, after, (9, 4, 5))
        after[contract.COUNTERS[1]] += 1
        with self.assertRaises(ValueError):
            contract.require_delta(before, after, (9, 4, 5))

    def test_missing_duplicate_metric_rejected(self):
        body = '\n'.join(name + ' 1' for name in contract.COUNTERS)
        self.assertEqual(len(contract.metric_values(body)), 3)
        for bad in ['', body + '\n' + contract.COUNTERS[0] + ' 1']:
            with self.assertRaises(ValueError):
                contract.metric_values(bad)

    def test_all_paths_and_prefill_progress(self):
        result = contract.validate_same_mode_paths(log())
        self.assertEqual(len(result['terminal_paths']), 8)
        self.assertEqual(result['prefill_cancel_position'], 8192)
        self.assertEqual(result['control_mtp_steps'], 74)

    def test_cross_mode_or_fallback_rejected(self):
        for old, new in [('path=mtp_multi_b1', 'path=plain'),
                         ('requested_mtp=1', 'requested_mtp=0'),
                         ('fallback=none', 'fallback=initialization'),
                         ('mtp_steps=74', 'mtp_steps=0'),
                         ('plain_tail_tokens=0', 'plain_tail_tokens=1')]:
            with self.assertRaises(ValueError):
                contract.validate_same_mode_paths(log().replace(old, new, 1))

    def test_missing_extra_duplicate_paths_rejected(self):
        lines = log().splitlines()
        for bad in ['\n'.join(lines[1:]), log() + lines[0] + '\n',
                    log() + lines[0].replace(contract.CONTROL, 'unexpected') + '\n']:
            with self.assertRaises(ValueError):
                contract.validate_same_mode_paths(bad)

    def test_changed_recovery_step_count_rejected(self):
        changed = log().replace('id=same-prefill-recovery requested_mtp=1 '
                                'path=mtp_multi_b1 mtp_steps=74',
                                'id=same-prefill-recovery requested_mtp=1 '
                                'path=mtp_multi_b1 mtp_steps=73')
        with self.assertRaises(ValueError):
            contract.validate_same_mode_paths(changed)

    def test_missing_or_wrong_interrupt_reason_rejected(self):
        for bad in [log().replace('reason=deadline_exceeded', 'reason=explicit_cancel'),
                    log().replace('id=same-decode-rst reason=', 'id=other reason=')]:
            with self.assertRaises(ValueError):
                contract.validate_same_mode_paths(bad)

    def test_missing_or_unbounded_prefill_progress_rejected(self):
        for old, new in [('position=8192', 'position=0'),
                         ('position=8192', 'position=45056'),
                         ('total=45056', 'total=1024'),
                         ('prefill cancelled', 'prefill completed')]:
            with self.assertRaises(ValueError):
                contract.validate_same_mode_paths(log().replace(old, new))

    def test_prefill_progress_must_be_adjacent_to_actual_request_id(self):
        progress = '[q4t] prefill cancelled seq=0 position=8192 total=45056\n'
        reason = '[q4t] request cancelled id=same-prefill-cancel reason=explicit_cancel\n'
        for bad in [log().replace(progress + reason, reason + progress),
                    log().replace(progress + reason, progress + '[q4t] other\n' + reason),
                    log().replace('id=same-prefill-cancel reason=', 'id=wrong reason=')]:
            with self.assertRaises(ValueError):
                contract.require_prefill_cancel(bad)


if __name__ == '__main__':
    unittest.main()
