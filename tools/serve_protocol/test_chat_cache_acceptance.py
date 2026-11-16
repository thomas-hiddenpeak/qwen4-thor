"""Acceptance guards require actual state evidence, not successful HTTP alone."""
import json
import unittest

from tools.examples.chat_client import Completion
from tools.serve_protocol import chat_cache_acceptance as a


def values():
    result = {}
    for label in a.requests():
        hit = label in ('hit', 'target_hit')
        content = (json.dumps(dict(answer=42)) if label.startswith('target_')
                   else '43' if label == 'hit' else '42')
        result[label] = Completion(id='cache-' + label, model='cpu',
            message=dict(role='assistant', content=content), finish_reason='stop',
            usage=dict(prompt_tokens=1400, completion_tokens=4, total_tokens=1404,
                       prompt_tokens_details=dict(cached_tokens=1211 if hit else 0)))
    return result


def log_rows(rows):
    log = []
    for label, value in rows.items():
        if label in ('hit', 'target_hit'):
            layout = 'target' if label.startswith('target_') else 'mtp'
            log.append(f'[session-cache] request={value.id} event=restore prefix=1211 '
                       f'ms=2 error=none source=shared layout={layout}')
        elif label != 'disabled':
            writes = [(1307, 1800)] if label == 'default_implicit' else [
                (1103, 43200), (1211, 1800)]
            log.extend(f'[prefix-cache] request={value.id} event=write prefix={prefix} '
                       f'ttl={ttl} saved=1 bytes=1234' for prefix, ttl in writes)
    return '\n'.join(log) + '\n[mtp-accept] request=' + rows['hit'].id


class CacheAcceptance(unittest.TestCase):
    def test_eight_requests_cover_default_explicit_isolation_and_target(self):
        rows = a.requests()
        self.assertEqual(a.LIMITS, dict(starts=1, chats=8, controls=2000, seconds=900))
        self.assertEqual(len(rows), 8)
        self.assertNotIn('prompt_cache_options', rows['default_implicit'])
        self.assertNotIn('prompt_cache_key', rows['default_implicit'])
        self.assertNotIn('prompt_cache_breakpoint', json.dumps(rows['disabled']))
        self.assertNotEqual(rows['write']['prompt_cache_key'], rows['key_isolated']['prompt_cache_key'])
        self.assertEqual(rows['write']['messages'][0]['content'][:2],
                         rows['hit']['messages'][0]['content'][:2])
        self.assertNotEqual(rows['write']['messages'][0]['content'][-1],
                            rows['hit']['messages'][0]['content'][-1])
        self.assertTrue(rows['target_hit']['stream_options']['include_usage'])

    def test_usage_and_content_must_match_the_actual_case(self):
        for label, value in values().items():
            a.check_answer(value, label)
        rows = values()
        rows['hit'].usage['prompt_tokens_details']['cached_tokens'] = 0
        with self.assertRaises(Exception):
            a.check_answer(rows['hit'], 'hit')
        rows = values()
        rows['hit'].message['content'] = '42'
        with self.assertRaises(Exception):
            a.check_answer(rows['hit'], 'hit')

    def test_missing_restore_failed_write_ttl_and_disabled_write_are_rejected(self):
        rows = values()
        log = log_rows(rows)
        self.assertEqual(len(a.check_cache_logs(log, rows)), 8)
        for changed in (
                log.replace('source=shared', 'source=private', 1),
                log.replace('saved=1', 'saved=0', 1),
                log.replace('ttl=43200', 'ttl=1800', 1),
                log + '\n[prefix-cache] request=cache-disabled event=write '
                      'prefix=1200 ttl=1800 saved=1 bytes=100'):
            with self.assertRaises(Exception):
                a.check_cache_logs(changed, rows)


if __name__ == '__main__':
    unittest.main()
