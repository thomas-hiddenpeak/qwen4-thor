"""Guard against accepting malformed, empty or cross-client projections."""
import copy
import unittest

from tools.serve_responses import include_projection_profiles as profile


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        proof = profile.package.read(profile.PROOF.parent/'device-sdk.json')
        text, scores = proof['selected'][0]
        self.revision = int(proof['answer'])
        part = dict(type='output_text', text=text, logprobs=scores)
        result = dict(id=proof['response'], status='completed',
                      output=[dict(type='message', content=[part])])
        self.row = dict(created=result, single=copy.deepcopy(result),
                        repeated=copy.deepcopy(result), peer_status=404)

    def test_real_sdk_projection(self):
        profile.check(self.row, self.revision)

    def test_different_owner_or_response(self):
        for key, value in [('peer_status', 200), ('id', 'resp_other'), ('status', 'failed')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.row)
                (row if key == 'peer_status' else row['repeated'])[key] = value
                profile.check(row, self.revision)

    def test_token_contract(self):
        for key, value in [('token', '?'), ('bytes', []), ('logprob', float('inf')),
                           ('logprob', True), ('top_logprobs', [])]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.row)
                _, tokens = profile.projection(row['created'])
                tokens[0][key] = value
                profile.check(row, self.revision)

    def test_changed_or_empty_projection(self):
        for key in ('created', 'repeated', 'single'):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.row)
                row[key]['output'][0]['content'][0]['logprobs'] = []
                profile.check(row, self.revision)
