"""API-specific Chat cache fields and exact content metadata contracts."""
import copy
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema, vision_schema


def request():
    return dict(messages=[dict(role='user', content=[dict(
        type='text', text='source', prompt_cache_breakpoint=dict(
            mode='explicit', ttl='12h'))])],
        prompt_cache_options=dict(mode='explicit', ttl='30m'))


class ChatCacheContract(unittest.TestCase):
    def test_chat_controls_have_their_own_closed_contract(self):
        for schema in (chat_schema(), vision_schema()):
            validator = Draft202012Validator(schema)
            for options in ({}, dict(mode='implicit'), dict(mode='explicit'),
                            dict(ttl='30m')):
                validator.validate(dict(request(), prompt_cache_options=options))
            for options in (None, [], dict(mode=None), dict(mode='unknown'),
                            dict(ttl='12h'), dict(ttl=None), dict(prewarm=False),
                            dict(comparison_response_id=None), dict(other=0)):
                self.assertFalse(validator.is_valid(dict(
                    request(), prompt_cache_options=options)), options)

    def test_text_roles_and_nested_breakpoint_lifetimes(self):
        validator = Draft202012Validator(chat_schema())
        for role in ('developer', 'system', 'user', 'assistant'):
            for ttl in ('30m', '12h'):
                row = request()
                row['messages'][0]['role'] = role
                marker = row['messages'][0]['content'][0]['prompt_cache_breakpoint']
                marker['ttl'] = ttl
                validator.validate(row)
                del marker['ttl']
                validator.validate(row)
        for marker in (None, {}, True, dict(mode='implicit'),
                       dict(mode='explicit', ttl=None),
                       dict(mode='explicit', extra=0)):
            row = request()
            row['messages'][0]['content'][0]['prompt_cache_breakpoint'] = marker
            self.assertFalse(validator.is_valid(row), marker)

    def test_native_scope_cannot_mix_with_standard_controls_or_markers(self):
        validator = Draft202012Validator(chat_schema())
        row = request()
        row['prefix_cache_scope'] = 'a' * 64
        self.assertFalse(validator.is_valid(row))
        del row['prompt_cache_options']
        self.assertFalse(validator.is_valid(row))
        del row['messages'][0]['content'][0]['prompt_cache_breakpoint']
        validator.validate(row)
        for field in ('prompt_cache_key', 'prompt_cache_retention'):
            validator.validate(dict(row, **{field: None}))

    def test_media_markers_are_structural_not_state_restore_evidence(self):
        validator = Draft202012Validator(vision_schema())
        for part in (dict(type='image_url', image_url='data:image/png;base64,AA=='),
                     dict(type='file', file=dict(file_id='file_example')),
                     dict(type='video_frames', video_frames=[
                         'data:image/png;base64,AA=='] * 2)):
            row = request()
            marked = copy.deepcopy(part)
            marked['prompt_cache_breakpoint'] = dict(mode='explicit', ttl='12h')
            row['messages'][0]['content'] = [marked]
            validator.validate(row)
            marked['prompt_cache_breakpoint'] = None
            self.assertFalse(validator.is_valid(row))


if __name__ == '__main__':
    unittest.main()
