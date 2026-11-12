"""Standalone moderation acceptance oracles; scores are explicit CPU fixtures."""
import copy
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses import moderations_acceptance as stage


class ModerationsAcceptanceTests(unittest.TestCase):
    def test_fixed_requests_profiles_and_generation_bound(self):
        rows = stage.requests()
        self.assertEqual(stage.LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(stage.MAX_MODEL_STEPS, 9)
        self.assertEqual(len(rows), 8)
        for name, value in rows.items():
            if name == 'chat': continue
            for request in value if name in ('batch', 'cancel') else [value]:
                self.assertEqual(stage.errors(request, 'moderations_create'), [], name)
        self.assertEqual(len(rows['text']['input']), 2)
        self.assertEqual(len(rows['cancel'][0]['input']), 8)
        self.assertNotIn('model', rows['text'])
        self.assertEqual(rows['multimodal']['input'][1], rows['image_only']['input'][0])
        policy = stage.Stage.__new__(stage.Stage).client_policy()
        self.assertEqual(policy['clients'][0]['max_output_tokens'], 64)
        self.assertEqual(policy['clients'][1]['tokens_per_minute'], 16)

    def test_complete_scores_require_actual_model_flags_and_modality(self):
        value = json.loads((Path(os.environ['Q4T_TEST_OUTPUT_DIR'])/
                            'moderations-response.json').read_text())
        value['model'] = stage.foreground.MODEL
        stage.check_response(value, [True, True])
        for field in ('partial', 'model', 'flag', 'types', 'cardinality'):
            bad = copy.deepcopy(value)
            if field == 'partial': bad['results'][0]['category_scores'].pop('violence')
            if field == 'model': bad['model'] = 'omni-moderation-latest'
            if field == 'flag': bad['results'][0]['flagged'] = False
            if field == 'types': bad['results'][0]['category_applied_input_types']['hate'] = []
            if field == 'cardinality': bad['results'].pop()
            with self.assertRaises(RuntimeError): stage.check_response(bad, [True, True])
        report = value['results'][0]
        for name in stage.contract.CATEGORIES:
            report['category_scores'][name] = 0
            report['categories'][name] = False
            report['category_applied_input_types'][name] = (
                ['image'] if name in stage.contract.IMAGE_CATEGORIES else [])
        report['flagged'] = False
        value['results'] = [report]
        stage.check_response(value, [False], text=False, image=True)
        report['category_scores']['hate'] = .1
        with self.assertRaises(RuntimeError):
            stage.check_response(value, [False], text=False, image=True)

    def test_actual_meter_refuses_extra_rpm_reservation_and_unaccounted_work(self):
        suffix = '{client="'+stage.encrypted.CLIENT+'"}'
        admitted = 'q4t_client_rate_admitted_total'+suffix
        reserved = 'q4t_client_rate_reserved_tokens'+suffix
        settled = 'q4t_client_rate_settled_tokens_total'+suffix
        value = {admitted:1, reserved:0, settled:43,
                 'q4t_prompt_tokens_total':30, 'q4t_generation_tokens_total':13}
        self.assertEqual(stage.check_meter({}, value)['total_tokens'], 43)
        for key, count in [(admitted, 2), (reserved, 1), (settled, 42)]:
            with self.assertRaises(RuntimeError):
                stage.check_meter({}, dict(value, **{key:count}))
        self.assertEqual(stage.check_meter(value, value, admitted=0)['total_tokens'], 0)
        with self.assertRaises(RuntimeError): stage.check_meter({}, value, admitted=0)


if __name__ == '__main__': unittest.main()
