"""The native Chat file shape and generated public contract; no model work."""
import copy
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import ROOT, vision_schema


class ChatFileContract(unittest.TestCase):
    def test_pdf_source_exclusivity_and_role(self):
        validator = Draft202012Validator(vision_schema())
        request = {'messages': [{'role': 'user', 'content': [
            {'type': 'file', 'file': {'file_id': 'file_original'}}]}]}
        self.assertTrue(validator.is_valid(request))
        variants = [dict(file_id='file_original', file_data='eA=='), {},
                    dict(file_url='https://example.com/file.pdf'),
                    dict(file_id=None), dict(file_data='eA==', filename=None)]
        for file in variants:
            changed = copy.deepcopy(request)
            changed['messages'][0]['content'][0]['file'] = file
            self.assertFalse(validator.is_valid(changed), file)
        for role in ('assistant', 'developer', 'system', 'tool'):
            changed = copy.deepcopy(request)
            changed['messages'][0]['role'] = role
            self.assertFalse(validator.is_valid(changed), role)

    def test_detail_and_published_schema_match_current_generator(self):
        schema = vision_schema()
        self.assertEqual(json.loads((ROOT/'docs/serve-vision-request-v1.schema.json').read_text()), schema)
        validator = Draft202012Validator(schema)
        for detail in ('auto', 'low', 'high', 'original'):
            request = {'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {
                    'url': 'https://example.com/image.webp', 'detail': detail}}]}]}
            self.assertTrue(validator.is_valid(request))
        request['messages'][0]['content'][0]['image_url']['detail'] = 'invented'
        self.assertFalse(validator.is_valid(request))


if __name__ == '__main__':
    unittest.main()
