"""Client composition and advertised media request surface, CPU only."""
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator
from tools.examples import chat_client as c, vision_query as q
from tools.vision_service import stage as s
from tools.serve_protocol import build_contract as contract


class Query(unittest.TestCase):
    def setUp(self):
        self.body=q.build_request('Read the table.',[s.BASE/'fixtures/document.png'])
        self.client=Mock()
        self.client.capabilities.return_value=dict(runtime=dict(media_enabled=True,mtp_enabled=False),
                                                  protocol_source_sha256='a'*64)
        self.client.preflight.return_value=dict(prompt_tokens=415,media=dict(token_ids='expanded_for_inference'))
        self.client.complete.return_value=c.Completion(id=self.body['request_id'],model=self.body['model'],
            message=dict(role='assistant',content='12'),finish_reason='stop',
            usage=dict(prompt_tokens=415,completion_tokens=3,total_tokens=418))

    def test_schema_and_serialized_result(self):
        Draft202012Validator(contract.vision_schema()).validate(self.body)
        result=q.query(self.client,self.body)
        self.assertEqual(result['completion']['message']['content'],'12')
        self.assertFalse(result['automatic_retry'])

    def test_discovery_stops_unsupported_profile_before_post(self):
        self.client.capabilities.return_value['runtime']['mtp_enabled']=True
        with self.assertRaises(c.ProtocolError):q.query(self.client,self.body)
        self.client.preflight.assert_not_called();self.client.complete.assert_not_called()

    def test_unexpanded_preflight_stops_before_inference(self):
        self.client.preflight.return_value['media']={}
        with self.assertRaises(c.ProtocolError):q.query(self.client,self.body)
        self.client.complete.assert_not_called()

    def test_inconsistent_usage_never_commits_success(self):
        self.client.preflight.return_value['prompt_tokens']=416
        with self.assertRaises(c.ProtocolError):q.query(self.client,self.body)

    def test_configurable_output_and_truncation_fail_closed(self):
        body=q.build_request('Read the table.',[s.BASE/'fixtures/document.png'],max_tokens=768)
        self.assertEqual(body['max_tokens'],768)
        self.client.complete.return_value=replace(self.client.complete.return_value,
                                                 finish_reason='length')
        with self.assertRaises(c.ProtocolError):q.query(self.client,self.body)
        self.assertEqual(self.client.complete.call_count,1)

    def test_cli_forwards_explicit_budget(self):
        import io
        argv=['vision_query','--image',str(s.BASE/'fixtures/document.png'),
              '--question','Read the table.','--max-tokens','768']
        with patch('sys.argv',argv),patch.object(q,'ChatClient'),\
                patch.object(q,'query',return_value={}) as query,\
                patch('sys.stdout',new_callable=io.StringIO):
            q.main()
        self.assertEqual(query.call_args.args[1]['max_tokens'],768)

    def test_schema_meaning_is_in_prompt_without_mutating_input(self):
        import copy
        import json
        schema = json.loads((s.ROOT/'tools/examples/vision_direction.schema.json').read_text())
        before = copy.deepcopy(self.body)
        changed = q.with_json_schema(self.body, schema)
        self.assertEqual(self.body, before)
        self.assertIn('moves RIGHT', changed['messages'][-1]['content'][-1]['text'])
        self.assertEqual(changed['messages'][-1]['content'][:-1], before['messages'][-1]['content'])
        schema['properties'].clear()
        self.assertIn('direction', changed['response_format']['json_schema']['schema']['properties'])
        Draft202012Validator(contract.vision_schema()).validate(changed)

    def test_schema_rejects_overwrite_and_excess_size(self):
        with self.assertRaises(c.ProtocolError):
            q.with_json_schema(dict(self.body,response_format={}),dict(type='object'))
        with self.assertRaises(c.ProtocolError):
            q.with_json_schema(self.body,dict(type='object',description='x'*65536))
        with self.assertRaises(c.ProtocolError):q.with_json_schema(self.body,dict(type='array'))

    def test_openapi_includes_explicit_media_for_both_routes(self):
        paths=contract.openapi()['paths']
        chat=paths['/v1/chat/completions']['post']['requestBody']['content']['application/json']['schema']
        pre=paths['/v1/chat/preflight']['post']['requestBody']['content']['application/json']['schema']['properties']['request']
        self.assertEqual(chat,pre)
        self.assertIn({'$ref':'serve-vision-request-v1.schema.json'},chat['anyOf'])
        self.assertIn('--no-mtp',chat['x-q4t-media-profile'])


if __name__=='__main__':unittest.main()
