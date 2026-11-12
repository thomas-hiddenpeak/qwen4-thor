"""Failure-sensitive cache business oracles; no model load or CUDA calls."""
import copy
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_responses.cache_acceptance import (
    LIMITS, ROOT, Stage, cache_usage, check_chat, check_restore, check_writes,
    requests)
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.test_contract import errors


def prewarm():
    return json.loads((ROOT / 'docs/evidence/serve-prompt-cache-cpu-20261110/'
        'cpu-01/cache-prewarm.json.wire').read_text())


def answer():
    value = json.loads((ROOT / 'docs/evidence/serve-prompts-business-20261110/'
        'first.sdk.json').read_text())['result']
    # Only oracle fixtures are adapted; these are not actual cache evidence.
    value['usage'].update(input_tokens=1400,
        total_tokens=1400 + value['usage']['output_tokens'],
        input_tokens_details=dict(cached_tokens=1300, cache_write_tokens=0))
    value['prompt_cache_diagnostics'] = dict(type='cache_hit')
    return value


class CacheAcceptance(unittest.TestCase):
    def test_frozen_requests_are_valid_and_separate_control_cases(self):
        self.assertEqual(LIMITS, dict(starts=1, chats=10, controls=2000, seconds=900))
        self.assertEqual(encrypted.Stage.requests_per_minute, 8)
        self.assertEqual(Stage.requests_per_minute, 16)
        rows = requests()
        self.assertEqual(len(rows), 10)
        for name, value in rows.items():
            if name.startswith('chat'):
                self.assertEqual(list(Draft202012Validator(chat_schema()).iter_errors(value)), [])
            else:
                self.assertEqual(errors(value, 'response_create'), [], name)
        self.assertTrue(rows['prewarm']['prompt_cache_options']['prewarm'])
        self.assertEqual(rows['prewarm']['input'], rows['hit']['input'])
        self.assertEqual(rows['target']['input'], rows['target_hit']['input'])
        self.assertTrue(rows['target_hit']['background'])
        self.assertNotEqual(rows['isolated']['prompt_cache_key'], rows['hit']['prompt_cache_key'])
        self.assertEqual(sum('prompt_cache_breakpoint' in p for p in
            rows['prewarm']['input'][0]['content']), 2)
        self.assertNotIn('prompt_cache_breakpoint', json.dumps(rows['disabled']))

    def test_actual_cpp_prewarm_requires_no_output_and_real_write_coverage(self):
        value = prewarm()
        cache_usage(value, prewarm=True, mode='write')
        for change in ('output', 'tokens', 'write', 'overlap'):
            bad = copy.deepcopy(value)
            if change == 'output':
                bad['output'] = answer()['output']
            elif change == 'tokens':
                bad['usage']['output_tokens'] = 1
                bad['usage']['total_tokens'] += 1
            elif change == 'write':
                bad['usage']['input_tokens_details']['cache_write_tokens'] = 0
            else:
                bad['usage']['input_tokens_details']['cached_tokens'] = 1024
            with self.assertRaises(RuntimeError, msg=change):
                cache_usage(bad, prewarm=True, mode='write')

    def test_hit_requires_answer_cache_usage_and_appropriate_diagnostic(self):
        value = answer()
        cache_usage(value, mode='hit', diagnostic='cache_hit')
        for change in ('answer', 'read', 'write', 'diagnostic'):
            bad = copy.deepcopy(value)
            if change == 'answer':
                next(v for v in bad['output'] if v['type'] == 'message')['content'][0]['text'] = '41'
            elif change == 'read':
                bad['usage']['input_tokens_details']['cached_tokens'] = 0
            elif change == 'write':
                bad['usage']['input_tokens_details']['cache_write_tokens'] = 1
            else:
                bad['prompt_cache_diagnostics']['type'] = 'unavailable'
            with self.assertRaises(RuntimeError, msg=change):
                cache_usage(bad, mode='hit', diagnostic='cache_hit')

    def test_physical_state_oracles_reject_false_counts_layout_ttl_and_failures(self):
        value = answer()
        log = ('[session-cache] request=' + value['id'] +
               ' event=restore prefix=1300 ms=1.0 error=none source=shared layout=mtp\n')
        check_restore(log, value, 'mtp')
        for bad in ('', log + log, log.replace('1300', '1299'),
                    log.replace('mtp', 'target'), log.replace('error=none', 'error=failed')):
            with self.assertRaises(RuntimeError):
                check_restore(bad, value, 'mtp')
        value = prewarm()
        prefix = '[prefix-cache] request=' + value['id'] + ' event=write '
        log = (prefix + 'prefix=1100 ttl=43200 saved=1 bytes=4096\n' +
               prefix + 'prefix=1300 ttl=1800 saved=1 bytes=8192\n')
        check_writes(log, value)
        for bad in ('', log.replace('saved=1', 'saved=0'),
                    log.replace('ttl=1800', 'ttl=43200'), log.replace('1300', '1299')):
            with self.assertRaises(RuntimeError):
                check_writes(bad, value)

    def test_standard_chat_cache_requires_real_hit_and_current_answer(self):
        value = json.loads((ROOT / 'docs/evidence/serve-prompts-business-20261110/'
            'same-pid-chat.wire').read_text())
        value['usage'].update(prompt_tokens=1400,
            total_tokens=1400 + value['usage']['completion_tokens'],
            prompt_tokens_details=dict(cached_tokens=1300))
        check_chat(value, hit=True)
        for change in ('answer', 'hit', 'total'):
            bad = copy.deepcopy(value)
            if change == 'answer':
                bad['choices'][0]['message']['content'] = '41'
            elif change == 'hit':
                bad['usage']['prompt_tokens_details']['cached_tokens'] = 0
            else:
                bad['usage']['total_tokens'] += 1
            with self.assertRaises(RuntimeError, msg=change):
                check_chat(bad, hit=True)


if __name__ == '__main__':
    unittest.main()
