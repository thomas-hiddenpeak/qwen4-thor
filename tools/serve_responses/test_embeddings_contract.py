"""Targeted native request/results and actual pinned SDK source checks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import unittest

from tools.serve_responses.test_contract import errors
from tools.serve_responses import embeddings_contract as contract
from tools.serve_protocol import build_contract


class EmbeddingsContract(unittest.TestCase):
    def test_input_families_and_precise_limits(self):
        for value in ['text', ['one', 'two'], [1, 2], [[1, 2], [3]]]:
            self.assertEqual(errors(dict(model='served', input=value), 'embeddings_create'), [])
        for value in ['', [], None, [True], ['x', [1]], [[], [1]], [-1], [1.2]]:
            self.assertTrue(errors(dict(model='served', input=value), 'embeddings_create'))
        for extra in [dict(dimensions=0), dict(dimensions=2561),
                      dict(encoding_format=None), dict(stream=False), dict(user=1)]:
            self.assertTrue(errors(dict(model='served', input='x', **extra), 'embeddings_create'))

    def test_actual_cpp_float_and_base64_fp32(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        floats = json.loads((root/'embeddings-float.json').read_text())
        encoded = json.loads((root/'embeddings-base64.json').read_text())
        for body in [floats, encoded]:
            self.assertEqual(errors(body, 'embeddings_response'), [])
            self.assertEqual(body['usage'], dict(prompt_tokens=3, total_tokens=3))
        for raw, actual in zip(encoded['data'], floats['data']):
            decoded = base64.b64decode(raw['embedding'], validate=True)
            self.assertEqual(decoded, struct.pack('<4f', *actual['embedding']))
            self.assertEqual(raw['index'], actual['index'])

    def test_sdk_identity_protocol_route_and_compiled_sources(self):
        sdk = Path(os.environ.get('Q4T_NODE_SDK_DIR', '.q4t-work/serve-protocol-20261103/sdk'))
        self.assertEqual(len(contract.source(sdk)), 2)
        operation = build_contract.openapi()['paths']['/v1/embeddings']['post']
        self.assertEqual(operation['operationId'], 'v1_embeddings')
        self.assertNotIn('text/event-stream', operation['responses']['200']['content'])
        from tools.serve_gateway.gateway import validate_routes
        validate_routes()
        root = Path(__file__).resolve().parents[2]
        sources = json.loads((root/'.q4t-work/serve-custom-tools-20261108/engine-build/generated/q4t/server/chat_protocol_sources.json').read_text())
        for name in ['embeddings_plan', 'embeddings_execution', 'embedding_metadata', 'chat_embeddings']:
            path = 'src/server/'+name+'.cpp'
            self.assertEqual(sources[path], hashlib.sha256((root/path).read_bytes()).hexdigest())
        self.assertIn('src/model/model_head.cu', sources)


if __name__ == '__main__':
    unittest.main()
