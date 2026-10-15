"""Synthetic contracts; no model, server or external fixture required."""
import unittest

from response_identity import response_identity


class ResponseIdentityTest(unittest.TestCase):
    def test_stream_chunks_share_actual_id(self):
        messages = [{'id': 'chatcmpl-auto-17', 'choices': [{'delta': part}]}
                    for part in [{'role': 'assistant'}, {'content': 'ok'}, {}]]
        result = response_identity(messages)
        self.assertTrue(result['response_id_valid'])
        self.assertEqual(result['response_id'], 'chatcmpl-auto-17')
        self.assertEqual(result['response_ids_observed'],
                         ['chatcmpl-auto-17'] * 3)

    def test_nonstream_response(self):
        self.assertTrue(response_identity([
            {'id': 'request_42', 'choices': [{'message': {'content': 'ok'}}]}
        ])['response_id_valid'])

    def test_usage_only_without_id_is_not_a_fabricated_id(self):
        messages = [{'id': 'r', 'choices': [{'delta': {}}]},
                    {'choices': [], 'usage': {'completion_tokens': 1}}]
        self.assertEqual(response_identity(messages)['response_ids_observed'],
                         ['r'])

    def test_missing_id_on_content_or_finish_rejected(self):
        for choice in [{'delta': {'content': 'bad'}}, {'finish_reason': 'stop'}]:
            result = response_identity([
                {'id': 'r', 'choices': [{'delta': {}}]}, {'choices': [choice]}])
            self.assertFalse(result['response_id_valid'])
            self.assertIsNone(result['response_id'])

    def test_mixed_ids_rejected_including_usage(self):
        for choices in [[], [{'delta': {'content': 'wrong request'}}]]:
            result = response_identity([
                {'id': 'r1', 'choices': [{'delta': {}}]},
                {'id': 'r2', 'choices': choices}])
            self.assertFalse(result['response_id_valid'])
            self.assertEqual(result['response_ids_observed'], ['r1', 'r2'])

    def test_no_recorded_identity_rejected(self):
        for messages in [[], [{'choices': [], 'usage': {}}]]:
            self.assertFalse(response_identity(messages)['response_id_valid'])

    def test_invalid_type_and_token_rejected_without_throwing(self):
        for value in [None, 7, [], {}, '', 'two words', '../path', '汉字',
                      'r\n', 'x' * 129]:
            with self.subTest(value=value):
                result = response_identity([{'id': value, 'choices': [{}]}])
                self.assertFalse(result['response_id_valid'])
                self.assertEqual(result['response_ids_observed'], [value])

    def test_maximum_length_id(self):
        self.assertTrue(response_identity([{'id': 'a' * 128}])[
            'response_id_valid'])


if __name__ == '__main__':
    unittest.main()
