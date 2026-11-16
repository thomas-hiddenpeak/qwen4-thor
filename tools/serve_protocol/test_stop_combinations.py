"""Published Chat/media schemas admit stop without weakening input validation."""
import copy
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema, vision_schema


class StopCombinationSchemaTests(unittest.TestCase):
    def test_legal_constraints_private_and_shared_combinations(self):
        for factory in (chat_schema, vision_schema):
            validator = Draft202012Validator(factory())
            base = dict(messages=[dict(role='user', content='query')],
                        stop=['END', '中'])
            for extra in (
                dict(response_format=dict(type='json_object')),
                dict(tool_choice='none'),
                dict(session_id='a'*64),
                dict(prefix_cache_scope='b'*64, prefix_cache_write_tokens=1024),
                dict(session_id='a'*64, response_format=dict(type='json_schema',
                    json_schema=dict(name='object', strict=True, schema=dict(
                        type='object', properties={}, required=[],
                        additionalProperties=False))))):
                validator.validate(dict(base, **extra))
            if factory is vision_schema:
                row = dict(base, response_format=dict(type='json_object'),
                           session_id='a'*64)
                row['messages'] = [dict(role='user', content=[
                    dict(type='text', text='query'),
                    dict(type='image_url', image_url=dict(
                        url='data:image/png;base64,AA=='))])]
                validator.validate(row)

    def test_invalid_stops_and_other_constraint_rules_still_fail(self):
        validator = Draft202012Validator(chat_schema())
        base = dict(messages=[dict(role='user', content='query')],
                    response_format=dict(type='json_object'), session_id='a'*64)
        for stop in ('', ['END', None], ['a']*5, True, 3, {}, 'a'*257):
            self.assertFalse(validator.is_valid(dict(base, stop=stop)))
        for stop in (None, [], 'END'):
            validator.validate(dict(base, stop=stop))
        for extra in (dict(parse_response=False), dict(tool_choice='required'),
                      dict(prefix_cache_scope='bad')):
            row = copy.deepcopy(base)
            row.update(extra, stop='END')
            self.assertFalse(validator.is_valid(row))


if __name__ == '__main__':
    unittest.main()
