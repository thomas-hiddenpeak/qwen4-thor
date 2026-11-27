"""CPU controls using the issued project's native embedding responses."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_preparation_requires_the_same_published_service_as_the_overlap_test(self):
        with tempfile.TemporaryDirectory() as temp:
            deployment = Path(temp)
            build = deployment/'build'
            manifest = build/'service/manifest.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(b'issued')
            binary = build/'q4t'
            binary.write_bytes(b'candidate')
            proof = deployment/'device.json'
            digest = suite.package.sha(manifest)
            suite.package.save(proof, dict(stage='native_reader_contention_on_primary',
                engine=suite.package.sha(binary), environment=dict(status='PASS'), service_manifest=digest))
            with patch.object(suite, 'PROOF', proof), patch.object(suite.lifecycle, 'prepare_from_manifest') as prepare:
                suite.prepare(deployment/'stage', build, deployment)
                self.assertEqual(prepare.call_args.args[3:6],
                                 (digest, suite.reuse.CURSOR_PAGE_REVIEWED, suite.KEY))
                manifest.write_bytes(b'different deployment')
                with self.assertRaisesRegex(RuntimeError, 'differs from the device test'):
                    suite.prepare(deployment/'stage', build, deployment)
                prepare.assert_called_once()
