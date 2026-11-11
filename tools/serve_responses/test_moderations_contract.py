"""Actual native standalone moderation shape and standard error boundaries."""
import json
import hashlib
import os
from pathlib import Path
import unittest
from tools.serve_responses.test_contract import errors
from tools.serve_responses import moderations_contract as contract
from tools.serve_protocol import build_contract


class ModerationsContract(unittest.TestCase):
    def test_standard_input_shapes_and_capacity_boundaries(self):
        good = [dict(input=''), dict(input=['one','two']), dict(input=[
            dict(type='text',text='context'), dict(type='image_url',
                image_url=dict(url='data:image/png;base64,AA=='))])]
        for value in good:
            self.assertEqual(errors(value,'moderations_create'), [])
        for value in [dict(input=[]),dict(input=None),dict(input=['one',{}]),
                      dict(input='x',stream=True),dict(input='x',model=None),
                      dict(input=['x']*33),dict(input=[dict(type='text',text='')]*257)]:
            self.assertTrue(errors(value,'moderations_create'))

    def test_actual_cpp_result_and_complete_score_map(self):
        value=json.loads((Path(os.environ['Q4T_TEST_OUTPUT_DIR'])/'moderations-response.json').read_text())
        self.assertEqual(errors(value,'moderations_response'),[])
        self.assertEqual(value['model'],'actual-qwen')
        self.assertEqual(len(value['results']),2)
        self.assertEqual(set(value),{'id','model','results'})
        for result in value['results']:
            self.assertEqual(set(result['category_scores']),set(contract.CATEGORIES))
        value['results'][0]['category_scores'].pop('violence')
        self.assertTrue(errors(value,'moderations_response'))

    def test_official_sdk_identity_and_openapi_route(self):
        sdk=Path(os.environ.get('Q4T_NODE_SDK_DIR',
            '.q4t-work/serve-protocol-20261103/sdk'))
        self.assertTrue(contract.source(sdk).is_file())
        operation=build_contract.openapi()['paths']['/v1/moderations']['post']
        self.assertEqual(operation['operationId'],'v1_moderations')
        self.assertIn('200',operation['responses'])
        self.assertNotIn('text/event-stream',operation['responses']['200']['content'])
        from tools.serve_gateway.gateway import validate_routes
        validate_routes()
        root = Path(__file__).resolve().parents[2]
        manifest = root/'.q4t-work/serve-custom-tools-20261108/engine-build/generated/q4t/server/chat_protocol_sources.json'
        sources = json.loads(manifest.read_text())
        for name in ['moderations_plan', 'moderations_execution', 'chat_moderations']:
            path = 'src/server/'+name+'.cpp'
            self.assertEqual(sources[path], hashlib.sha256((root/path).read_bytes()).hexdigest())
        for name in ['moderations_plan', 'moderations_execution']:
            self.assertIn('include/q4t/server/'+name+'.h', sources)


if __name__=='__main__': unittest.main()
