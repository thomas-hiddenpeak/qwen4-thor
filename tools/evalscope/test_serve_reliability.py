"""Client acceptance must reject corrupt UTF-8 and false/duplicate terminals."""
import json
import unittest

from serve_reliability import decoded, fields


def event(value):
    return b'data: '+json.dumps(value, ensure_ascii=False).encode()+b'\r\n\r\n'


def response(wire):
    return dict(stream=True, status=200, wire_hex=wire.hex())


class WireTests(unittest.TestCase):
    def test_request_intervals_exclude_startup_policy_banners(self):
        raw = ('[q4t][residency] chunk_order=0 policy=original\n'
               '[q4t][residency] partition=0 policy=original\n'
               '[q4t][residency] id=req scope=global_interval interval=1 loads=2\n')
        rows = fields(raw, '[q4t][residency] id=')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'req')
        self.assertEqual(rows[0]['interval'], '1')

    def test_complete_unicode_and_usage(self):
        wire = event(dict(choices=[dict(delta=dict(content='你好🚀'),
                                       finish_reason=None)]))
        wire += event(dict(choices=[dict(delta={}, finish_reason='stop')],
                           usage=dict(prompt_tokens=1024, completion_tokens=4)))
        wire += b'data: [DONE]\r\n\r\n'
        self.assertEqual(decoded(response(wire))['text'], '你好🚀')
        with self.assertRaises(AssertionError):
            decoded(response(wire+b'data: [DONE]\r\n\r\n'))

    def test_each_event_must_be_utf8(self):
        wire = b'data: {"choices":[{"delta":{"content":"\xe4\xbd"}}]}\n\n'
        wire += b'data: [DONE]\n\n'
        with self.assertRaises(UnicodeDecodeError):
            decoded(response(wire))

    def test_error_cannot_be_success_or_normal_finish(self):
        error = event(dict(error=dict(message='cancelled')))
        done = b'data: [DONE]\n\n'
        self.assertTrue(decoded(response(error+done), False)['errors'])
        with self.assertRaises(AssertionError):
            decoded(response(error+done))
        terminal = event(dict(choices=[dict(delta={}, finish_reason='length')]))
        with self.assertRaises(AssertionError):
            decoded(response(error+terminal+done), False)
        with self.assertRaises(AssertionError):
            decoded(response(error), False)


if __name__ == '__main__':
    unittest.main()
