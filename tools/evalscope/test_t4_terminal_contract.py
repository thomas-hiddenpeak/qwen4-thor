"""Synthetic parser counterexamples; these never execute CUDA or HTTP."""
import json
import unittest

import mtp_t4_terminal_contract as contract


def event(name, ordinal, **fields):
    values = {'ordinal': ordinal, 'real_ok': 1, **fields}
    return '[q4t][t4_terminal_' + name + '] ' + ' '.join(
        f'{key}={value}' for key, value in values.items())


def valid_log():
    lines = [contract.VARIANT]
    for ordinal, (label, stop_row, cap, _) in enumerate(contract.PLAN, 1):
        controlled = stop_row >= 0 or cap == 5
        consumed = stop_row + 1 if stop_row >= 0 else cap - 1
        lines.append(event('begin', ordinal, slot=0, stage='prefill',
                           position=0, history=0, pending=0))
        lines.append(event('prefill', ordinal, rows=1024, prompt_captured=1,
                           completion='caller_owned'))
        if controlled:
            lines.append(event('verify', ordinal, batch=1, rows=4,
                               position=1024, selected=0))
            chosen = [20, 21, 22, 23]
            prefix = stop_row if stop_row >= 0 else 3
            chosen[:prefix] = [10, 11, 12][:prefix]
            chosen[prefix] = 99 if stop_row >= 0 else 8
            lines.append(event('select', ordinal, stop_row=stop_row,
                               stop_id=99, stop_count=2, bonus=8,
                               draft='10,11,12', original='20,21,22,23',
                               selected=','.join(map(str, chosen)),
                               changed_after_real_d2h=1))
            if 0 <= stop_row < 3:
                lines.append(event('restore', ordinal, checkpoint=stop_row,
                                   slot=0))
            if cap == 5:
                lines.append(event('extend', ordinal, rows=4))
            lines.append(event('return', ordinal, counts=consumed,
                               next_b=chosen[prefix],
                               next_d0=-1 if stop_row >= 0 else 42,
                               terminal=int(stop_row >= 0), caller_unchanged=1,
                               prefix_nonstop=1, pending=0,
                               restores=int(0 <= stop_row < 3),
                               extends=int(cap == 5), observer_cuda_calls=0,
                               next_g='invalid_not_read' if stop_row >= 0
                               else 'valid_not_inspected'))
        path = ('mtp_multi_b1' if controlled else 'prefill_only' if cap == 1
                else 'plain_tail_b1')
        tail = 1 if cap == 5 else cap if 2 <= cap <= 4 else 0
        lines.append(f'[q4t][decode_path] id={label} requested_mtp=1 '
                     f'path={path} mtp_steps={int(controlled)} fallback=none '
                     f'plain_tail_tokens={tail} verifier=t4 '
                     f'tail_reason={"output_limit" if tail else "none"}')
        lines.append(event('end', ordinal, committed_position=1024 + consumed,
                           committed_history=1024 + consumed, history_exact=1,
                           steps=int(controlled),
                           restores=int(0 <= stop_row < 3),
                           extends=int(cap == 5),
                           ordinary_calls=cap - 1 if 1 <= cap <= 4 else 0,
                           stage='idle', position=0, history=0, pending=0))
    return '\n'.join(lines) + '\n'


def response(label, output, finish, stream):
    usage = {'prompt_tokens': 1024, 'completion_tokens': output,
             'total_tokens': 1024 + output}
    if stream:
        events = [{'id': label, 'choices': [
            {'delta': {'content': 'x'}, 'finish_reason': None}]},
            {'id': label, 'choices': [
                {'delta': {}, 'finish_reason': finish}]},
            {'id': label, 'choices': [], 'usage': usage}]
        body = ''.join('data: ' + json.dumps(row) + '\n\n' for row in events)
        body += 'data: [DONE]\n\n'
    else:
        body = json.dumps({'id': label, 'choices': [
            {'message': {'content': 'x'}, 'finish_reason': finish}],
            'usage': usage})
    return {'status': 200, 'body': body, 'headers': []}


class TerminalContractTest(unittest.TestCase):
    def test_all_nine_log_cases(self):
        result = contract.validate_log(valid_log())
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['groups']), 9)

    def test_all_nine_http_cases(self):
        for label, stop_row, cap, stream in contract.PLAN:
            with self.subTest(label=label):
                output = stop_row + 2 if stop_row >= 0 else cap
                actual = response(label, output,
                                  'stop' if stop_row >= 0 else 'length', stream)
                self.assertFalse(contract.completed_result(
                    actual, label, stop_row, cap, stream)['errors'])

    def test_terminal_and_tail_counterexamples(self):
        original = valid_log()
        replacements = (
            ('checkpoint=0 slot=0', 'checkpoint=1 slot=0'),
            ('next_d0=-1 terminal=1', 'next_d0=42 terminal=1'),
            ('caller_unchanged=1', 'caller_unchanged=0'),
            ('prefix_nonstop=1', 'prefix_nonstop=0'),
            ('changed_after_real_d2h=1', 'changed_after_real_d2h=0'),
            ('selected=99,21,22,23', 'selected=99,21,22,24'),
            ('stop_id=99', 'stop_id=98'),
            ('stop_count=2', 'stop_count=0'),
            ('committed_position=1025', 'committed_position=1026'),
            ('history_exact=1', 'history_exact=0'),
            ('observer_cuda_calls=0', 'observer_cuda_calls=1'),
            ('path=prefill_only mtp_steps=0', 'path=mtp_multi_b1 mtp_steps=1'),
            ('path=plain_tail_b1', 'path=plain'),
            ('plain_tail_tokens=2', 'plain_tail_tokens=1'),
            ('tail_reason=output_limit', 'tail_reason=none'),
            ('ordinary_calls=3', 'ordinary_calls=4'),
            ('extends=1 ordinary_calls=0', 'extends=1 ordinary_calls=1'),
            ('next_g=invalid_not_read', 'next_g=valid_not_inspected'),
            ('slot=0 stage=prefill', 'slot=1 stage=prefill'),
        )
        for before, after in replacements:
            with self.subTest(before=before):
                self.assertIn(before, original)
                with self.assertRaises(ValueError):
                    contract.validate_log(original.replace(before, after, 1))

    def test_only_reachable_draft_stop_prevents_coverage(self):
        original = valid_log()
        unreachable = original.replace('draft=10,11,12', 'draft=99,11,12', 1)
        self.assertTrue(contract.validate_log(unreachable)['passed'])
        lines = original.splitlines()
        lines = [line.replace('draft=10,11,12', 'draft=99,11,12')
                 if line.startswith('[q4t][t4_terminal_select] ordinal=2 ')
                 else line for line in lines]
        with self.assertRaises(ValueError):
            contract.validate_log('\n'.join(lines))

    def test_missing_real_verify(self):
        text = valid_log()
        text = '\n'.join(line for line in text.splitlines()
                         if not line.startswith('[q4t][t4_terminal_verify] '
                                                'ordinal=1 '))
        with self.assertRaises(ValueError):
            contract.validate_log(text)

    def test_illegal_terminal_extend(self):
        text = valid_log().replace('[q4t][t4_terminal_return] ordinal=1 ',
                                  event('extend', 1, rows=1) + '\n'
                                  '[q4t][t4_terminal_return] ordinal=1 ', 1)
        with self.assertRaises(ValueError):
            contract.validate_log(text)

    def test_duplicate_field_and_unknown_event(self):
        for text in (valid_log().replace('ordinal=1 ', 'ordinal=1 ordinal=1 ', 1),
                     valid_log() + '[q4t][t4_terminal_unknown] ordinal=9\n'):
            with self.subTest(text=text[-100:]):
                with self.assertRaises(ValueError):
                    contract.validate_log(text)

    def test_missing_and_extra_request(self):
        for text in (valid_log().replace('id=t4-cap-5 ', 'id=other ', 1),
                     valid_log() +
                     '[q4t][decode_path] id=probe requested_mtp=1\n'):
            with self.assertRaises(ValueError):
                contract.validate_log(text)

    def test_response_wrong_id_count_finish_and_status(self):
        for stream in (False, True):
            for actual in (response('other', 2, 'stop', stream),
                           response('t4-stop-0', 1, 'stop', stream),
                           response('t4-stop-0', 2, 'length', stream),
                           {'status': 500, 'body': '{}'}):
                with self.subTest(stream=stream, response=actual):
                    with self.assertRaises(ValueError):
                        contract.completed_result(actual, 't4-stop-0', 0,
                                                  256, stream)

    def test_tail_natural_early_eos_is_coverage_failure(self):
        actual = response('t4-cap-4', 3, 'stop', False)
        with self.assertRaises(ValueError):
            contract.completed_result(actual, 't4-cap-4', -1, 4, False)

    def test_stream_requires_one_done(self):
        actual = response('t4-stop-0', 2, 'stop', True)
        for body in (actual['body'].replace('data: [DONE]\n\n', ''),
                     actual['body'] + 'data: [DONE]\n\n'):
            with self.assertRaises(ValueError):
                contract.completed_result({**actual, 'body': body},
                                          't4-stop-0', 0, 256, True)


if __name__ == '__main__':
    unittest.main()
