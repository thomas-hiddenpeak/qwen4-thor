"""CPU checks of media governance evidence interpretation, no listeners."""
import json
import unittest
from pathlib import Path

from tools.vision_service import governance as g
from tools.vision_service import stage as s


class Governance(unittest.TestCase):
    def test_real_policy_has_isolated_expansion_boundary(self):
        value=g.policy();g.cp.validate(value)
        self.assertTrue(value['fair_queue'])
        a,b=value['clients']
        self.assertEqual(a['requests_per_minute'],6)
        self.assertGreater(a['max_inflight'],4)
        self.assertEqual(b['tokens_per_minute'],1000)
        self.assertGreater(a['max_body_bytes'],2*1024**2)

    def test_metric_must_be_present_once_and_client_specific(self):
        raw='q4t_client_rate_reserved_tokens{client="alpha"} 123\n'
        self.assertEqual(g.rate(raw,'alpha','reserved_tokens'),123)
        with self.assertRaises(RuntimeError):g.rate(raw,'beta','reserved_tokens')
        with self.assertRaises(RuntimeError):g.rate(raw+raw,'alpha','reserved_tokens')

    def test_existing_cancel_wire_uses_public_error_contract(self):
        folder=s.BASE/'phase-04/execution'
        body=json.loads((folder/'cancelled-visual-request.json').read_text())['body']
        value=g.cancelled_wire((folder/'cancelled-visual-wire.bin').read_bytes(),body['request_id'])
        self.assertEqual(value['error_code'],'explicit_cancel')

    def test_json_object_allows_additional_semantic_fields(self):
        # Generic JSON object is not an additionalProperties:false schema.
        folder=s.BASE/'phase-04/execution'
        request=json.loads((folder/'image-json-object-request.json').read_text())['body']
        result=json.loads((folder/'image-json-object-completion.json').read_text())
        self.assertEqual(request['response_format'],{'type':'json_object'})
        self.assertEqual(json.loads(result['message']['content'])['color'],'red')
        self.assertEqual(result['finish_reason'],'stop')


if __name__=='__main__':unittest.main()
