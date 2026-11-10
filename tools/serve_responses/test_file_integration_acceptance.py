"""CPU checks for bounded file business requests and rejection oracles."""
import base64
import copy
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import build_contract as api
from tools.serve_responses import file_integration_acceptance as driver
from tools.serve_responses.test_contract import errors


class FileIntegrationAcceptance(unittest.TestCase):
    def test_requests_profiles_and_original_pdf_identity(self):
        self.assertEqual(driver.LIMITS, dict(starts=3, chats=7, controls=2000, seconds=1200))
        self.assertEqual(driver.MODEL_STEPS, 9)
        rows = driver.requests()
        for name in ('pdf', 'chat'):
            chat = Draft202012Validator(api.vision_schema() if name == 'pdf'
                                         else api.chat_schema())
            self.assertEqual(list(chat.iter_errors(rows[name])), [])
        for name in ('csv_stream', 'follow'):
            self.assertEqual(errors(rows[name], 'response_create'), [])
            self.assertEqual(errors(driver.tokens.count_request(rows[name]),
                                    'input_tokens_request'), [])
        encoded = rows['pdf']['messages'][0]['content'][0]['file']['file_data']
        self.assertEqual(base64.b64decode(encoded, validate=True),
                         (driver.ROOT/driver.PDF).read_bytes())
        stage = object.__new__(driver.Stage)
        for starts in (0, 1, 2):
            stage.counts = dict(starts=starts)
            self.assertEqual('--no-mtp' in stage.extra_model_args(), starts >= 1)
            self.assertEqual('--allow-media' in stage.extra_model_args(), starts >= 1)
            self.assertEqual(stage.client_policy()['clients'][0]['max_body_bytes'],
                             (16 if starts == 2 else 96)*1024**2)

    def test_pdf_oracle_requires_real_number_color_and_complete_answer(self):
        def value(number=42, color='green', finish='stop'):
            return dict(choices=[dict(finish_reason=finish,
                message=dict(content=json.dumps(dict(value=number, color=color))))])
        driver.pdf_answer(value())
        for bad in (value(number=41), value(color='red'), value(finish='length')):
            with self.assertRaises(RuntimeError):
                driver.pdf_answer(bad)

    def test_count_usage_headers_are_complete_strict_and_absent_for_frozen(self):
        headers = {'X-Q4T-Preparation-Input-Tokens': '123',
                   'X-Q4T-Preparation-Output-Tokens': '45',
                   'X-Q4T-Preparation-Model-Steps': '1'}
        self.assertEqual(driver.preparation_usage(dict(headers=headers), 1),
                         dict(input_tokens=123, output_tokens=45))
        self.assertEqual(driver.preparation_usage(dict(headers={}), 0),
                         dict(input_tokens=0, output_tokens=0))
        with self.assertRaises(RuntimeError):
            driver.preparation_usage(dict(headers=headers), 0)
        for name in headers:
            for bad in (None, '', '-1', '0', '1.0', '１２', True):
                value = dict(headers)
                if bad is None:
                    del value[name]
                else:
                    value[name] = bad
                with self.assertRaises(RuntimeError):
                    driver.preparation_usage(dict(headers=value), 1)

    def test_public_stored_message_projection_restores_only_original_input(self):
        expected = driver.requests()['pdf']['messages'][0]
        message = dict(id='chatmsg_fixture', role='user', name=None,
            content='public text projection', content_parts=copy.deepcopy(expected['content']))
        page = dict(data=[message], has_more=False)
        restored = driver.replay_chat_messages(page, expected)
        self.assertEqual(restored, [expected])
        self.assertNotIn('id', restored[0])
        restored[0]['content'][0]['file']['file_data'] = 'changed'
        self.assertEqual(driver.replay_chat_messages(page, expected), [expected])
        wrong = copy.deepcopy(page)
        wrong['data'][0]['content_parts'][0]['file']['file_data'] = 'changed'
        with self.assertRaises(RuntimeError):
            driver.replay_chat_messages(wrong, expected)
        with self.assertRaises(RuntimeError):
            driver.replay_chat_messages(dict(page, has_more=True), expected)


if __name__ == '__main__':
    unittest.main()
