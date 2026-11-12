"""Reject false retrieval acceptance using actual captured CPU producer output."""
import copy
import json
import unittest

from tools.serve_responses import file_search_acceptance as stage


class FileSearchAcceptanceTests(unittest.TestCase):
    def test_actual_wire_oracle_rejects_wrong_source_index_and_projection(self):
        root = stage.ROOT/'docs/evidence/serve-responses-file-search-cpu-20261112/native-file-search-sdk'
        for name in ('included', 'hidden'):
            value = json.loads((root/(name+'.json')).read_text())
            file = value['output'][1]['content'][0]['annotations'][0]['file_id']
            stage.check_answer(value, file, included=name == 'included', expected_terms=('SSD',))
            with self.assertRaisesRegex(RuntimeError, 'actual.*source|source citation'):
                stage.check_answer(value, 'file_wrong', included=name == 'included')
            for field in ('index', 'status', 'queries'):
                bad = copy.deepcopy(value)
                if field == 'index':
                    bad['output'][1]['content'][0]['annotations'][0]['index'] -= 1
                elif field == 'status':
                    bad['output'][0]['status'] = 'incomplete'
                else:
                    bad['output'][0]['queries'].append('unplanned additional query')
                with self.subTest(name=name, field=field), self.assertRaises(RuntimeError):
                    stage.check_answer(bad, file, included=name == 'included', expected_terms=('SSD',))
        hidden = copy.deepcopy(value)
        hidden['output'][0]['results'] = []
        with self.assertRaisesRegex(RuntimeError, 'include projection'):
            stage.check_answer(hidden, file, included=False, expected_terms=('SSD',))

    def test_meter_rejects_duplicate_rpm_lost_query_usage_and_retained_reservation(self):
        suffix = '{client="'+stage.encrypted.CLIENT+'"}'
        admission = 'q4t_client_rate_admitted_total'+suffix
        reserved = 'q4t_client_rate_reserved_tokens'+suffix
        settled = 'q4t_client_rate_settled_tokens_total'+suffix
        metrics = {admission:1, reserved:0, settled:107,
                   'q4t_prompt_tokens_total':100, 'q4t_generation_tokens_total':7}
        response = dict(usage=dict(input_tokens=100, output_tokens=7, total_tokens=107))
        stage.check_meter({}, metrics, response)
        for key, value in [(admission, 2), (reserved, 1), (settled, 106),
                           ('q4t_prompt_tokens_total', 90), ('q4t_generation_tokens_total', 8)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                stage.check_meter({}, dict(metrics, **{key:value}), response)

    def test_business_requests_conform_to_pinned_application_contract(self):
        for name, request in stage.requests().items():
            if name == 'chat':
                continue
            kind = 'vector_search_request' if name == 'search' else 'response_create'
            self.assertFalse(stage.errors(request, kind), (name, stage.errors(request, kind)))


if __name__ == '__main__':
    unittest.main()
