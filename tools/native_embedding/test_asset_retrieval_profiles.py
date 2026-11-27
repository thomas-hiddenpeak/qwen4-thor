"""CPU controls using the issued project's native embedding responses."""
import copy
import unittest

from tools.native_embedding import asset_retrieval_profiles as suite


class ProjectRetrievalTests(unittest.TestCase):
    def setUp(self):
        prior = suite.package.read(suite.CORPUS/'default.wire')
        self.response = dict(prior, data=[dict(prior['data'][i], index=j)
                                          for j, i in enumerate(suite.INDICES)])

    def test_issued_vectors_retrieve_gateway_and_monitoring_passages(self):
        inputs = suite.inputs()
        self.assertIn('nginx', inputs[0])
        self.assertIn('观测', inputs[1])
        self.assertIn('nginx', inputs[2])
        self.assertIn('观测', inputs[3])
        scores = suite.check(self.response)['cosine_scores']
        self.assertGreater(scores[0][0], scores[0][1])
        self.assertGreater(scores[1][1], scores[1][0])

    def test_rejects_swapped_document_or_query_vectors_with_valid_wire_indices(self):
        for left, right in [(0,1), (2,3)]:
            with self.subTest(left=left), self.assertRaisesRegex(RuntimeError, 'vector differs'):
                response = copy.deepcopy(self.response)
                rows = response['data']
                rows[left]['embedding'], rows[right]['embedding'] = (
                    rows[right]['embedding'], rows[left]['embedding'])
                suite.check(response)

    def test_rejects_missing_and_nonfinite_components(self):
        for vector in ([0.0], [float('inf')]*1024):
            response = copy.deepcopy(self.response)
            response['data'][0]['embedding'] = vector
            with self.subTest(length=len(vector)), self.assertRaises(RuntimeError):
                suite.check(response)
