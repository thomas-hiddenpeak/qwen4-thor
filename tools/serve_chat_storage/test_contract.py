"""Validate real CPU HTTP records and public declarations, without a GPU."""
import json
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_gateway import gateway
from tools.serve_protocol import build_contract as protocol
from tools.serve_responses.test_contract import errors

ROOT = gateway.ROOT


class StoredChatContract(unittest.TestCase):
    def test_creation_and_metadata_bounds_match_native_contract(self):
        schema = protocol.chat_schema()
        Draft202012Validator.check_schema(schema)
        validate = Draft202012Validator(schema)
        prompt = dict(messages=[dict(role='user', content='资料')])
        for options in [dict(store=True, metadata={'资料': '✅'*512}),
                        dict(store=None, metadata=None), dict(store=False)]:
            validate.validate(dict(prompt, **options))
        for options in [dict(store='true'), dict(metadata={'a': 1}),
                        dict(metadata={'a': '✅'*513}),
                        dict(metadata={'✅'*65: 'value'})]:
            self.assertFalse(validate.is_valid(dict(prompt, **options)))
        many = dict(messages=[dict(role='user', content='x')]*4097)
        self.assertTrue(validate.is_valid(many))
        self.assertFalse(validate.is_valid(dict(many, store=True)))
        self.assertFalse(errors(dict(metadata=None), 'stored_chat_update'))
        self.assertTrue(errors({}, 'stored_chat_update'))
        self.assertTrue(errors(dict(metadata={}, store=True), 'stored_chat_update'))

    def test_actual_sdk_http_resources_match_declared_shapes(self):
        checked = {}
        paths = sorted(path for root in os.environ['Q4T_CHAT_STORAGE_WIRE'].split(os.pathsep)
                       for path in Path(root).rglob('requests.jsonl'))
        self.assertEqual(len(paths), 4)
        for path in paths:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                value = json.loads(row['response'])
                tag = value.get('object')
                name = ('stored_chat' if tag == 'chat.completion' else
                        'stored_chat_deleted' if tag == 'chat.completion.deleted' else
                        'stored_chat_messages' if tag == 'list' and '/messages' in row['target'] else
                        'stored_chat_list' if tag == 'list' else None)
                if name:
                    failures = errors(value, name)
                    self.assertFalse(failures, (str(path), row['target'],
                        [(list(e.absolute_path), e.message[:200]) for e in failures]))
                    checked[name] = checked.get(name, 0) + 1
        self.assertGreater(checked['stored_chat'], 10)
        self.assertGreater(checked['stored_chat_messages'], 4)
        self.assertGreater(checked['stored_chat_list'], 4)
        self.assertEqual(checked['stored_chat_deleted'], 1)
        out = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        out.mkdir(parents=True, exist_ok=True)
        (out/'wire-shape-counts.json').write_text(json.dumps(checked, indent=2)+'\n')

    def test_optional_nginx_and_formal_application_routes_are_complete(self):
        spec = protocol.openapi()
        expected = {(method.upper(), path) for path, operations in spec['paths'].items()
            if path.startswith(('/v1/chat/completions', '/v1/responses', '/v1/conversations'))
            for method in operations} - {('POST', '/v1/chat/completions')}
        self.assertEqual({(method, path) for method, path, _ in gateway.APPLICATION_ROUTES}, expected)
        self.assertTrue(gateway.validate_routes())
        listing = spec['paths']['/v1/chat/completions']['get']
        metadata = next(p for p in listing['parameters'] if p['name'] == 'metadata')
        self.assertEqual(metadata['style'], 'deepObject')
        self.assertTrue(metadata['explode'])
        update = spec['paths']['/v1/chat/completions/{completion_id}']['post']
        self.assertEqual(update['requestBody']['content']['application/json']['schema']['$ref'],
                         'serve-application-v1.schema.json#/$defs/stored_chat_update')


if __name__ == '__main__':
    unittest.main()
