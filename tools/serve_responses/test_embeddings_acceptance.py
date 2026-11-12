"""Bounded embedding acceptance oracles; no model or GPU execution."""
import base64
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from tools.serve_responses import embeddings_acceptance as stage


class EmbeddingsAcceptanceTests(unittest.TestCase):
    def test_real_size_tokenizer_asset_is_separate_from_package_metadata(self):
        with tempfile.TemporaryDirectory(dir=stage.ROOT/'.q4t-work') as directory:
            path = Path(directory)/'tokenizer.json'
            value = dict(model=dict(vocab={'!':0}), padding=' '*(2*1024**2))
            path.write_text(json.dumps(value))
            stage.verify_raw_token(path)
            value['model']['vocab']['!'] = 1
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, 'raw token fixture'):
                stage.verify_raw_token(path)
            with path.open('wb') as stream: stream.truncate(64*1024**2+1)
            with self.assertRaisesRegex(RuntimeError, 'asset size bound'):
                stage.verify_raw_token(path)

    def test_fixed_requests_include_raw_inputs_and_bounded_work(self):
        rows = stage.requests()
        self.assertEqual(stage.LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(len(rows), 8)
        for name, value in rows.items():
            if name in ('chat', 'invalid'): continue
            for request in value if name in ('batch', 'cancel') else [value]:
                self.assertEqual(stage.errors(request, 'embeddings_create'), [], name)
        self.assertTrue(stage.errors(rows['invalid'], 'embeddings_create'))
        self.assertEqual(rows['default']['input'][0], '!')
        self.assertEqual(rows['ids']['input'], [[0]])
        self.assertEqual(rows['reduced']['input'], [0])
        self.assertEqual(sum(len(row) for row in rows['cancel'][0]['input']), 16384)
        self.assertEqual(6 + 1 + 1 + 2 + 2*4, stage.MAX_EMBEDDING_CHUNKS)
        policy = stage.Stage.__new__(stage.Stage).client_policy()
        self.assertEqual(policy['clients'][1]['tokens_per_minute'], 16)

    def test_vector_oracle_rejects_nonfinite_unnormalized_and_misordered_output(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        value = json.loads((root/'embeddings-float.json').read_text())
        value['model'] = stage.foreground.MODEL
        value['data'] = value['data'][:1]
        self.assertEqual(stage.vectors(value, 1, 4), [[0.6, 0.8, 0, 0]])
        encoded = copy.deepcopy(value)
        encoded['data'][0]['embedding'] = base64.b64encode(
            struct.pack('<4f', .6, .8, 0, 0)).decode()
        self.assertEqual(struct.pack('<4f', *stage.vectors(encoded, 1, 4)[0]),
                         struct.pack('<4f', .6, .8, 0, 0))
        for change in ('model', 'index', 'dimension', 'norm', 'nonfinite', 'usage', 'bytes'):
            bad = copy.deepcopy(value)
            if change == 'model': bad['model'] = 'not-served'
            if change == 'index': bad['data'][0]['index'] = 1
            if change == 'dimension': bad['data'][0]['embedding'].pop()
            if change == 'norm': bad['data'][0]['embedding'] = [0, 0, 0, 0]
            if change == 'nonfinite': bad['data'][0]['embedding'][0] = float('nan')
            if change == 'usage': bad['usage']['total_tokens'] += 1
            if change == 'bytes': bad['data'][0]['embedding'] = 'AAAA'
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                stage.vectors(bad, 1, 4)

    def test_embedding_meter_requires_one_rpm_zero_output_and_exact_actual_input(self):
        suffix = '{client="'+stage.encrypted.CLIENT+'"}'
        admitted = 'q4t_client_rate_admitted_total'+suffix
        reserved = 'q4t_client_rate_reserved_tokens'+suffix
        settled = 'q4t_client_rate_settled_tokens_total'+suffix
        value = {admitted:1, reserved:0, settled:3,
                 'q4t_prompt_tokens_total':3, 'q4t_generation_tokens_total':0}
        self.assertEqual(stage.meter({}, value), 3)
        self.assertEqual(stage.meter(value, value, admitted=0), 0)
        for key, count in [(admitted, 2), (reserved, 1), (settled, 2),
                           ('q4t_generation_tokens_total', 1)]:
            with self.assertRaises(RuntimeError):
                stage.meter({}, dict(value, **{key:count}))


if __name__ == '__main__': unittest.main()
