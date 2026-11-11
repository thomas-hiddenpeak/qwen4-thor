"""Negative controls for the numerical/retrieval decision, no model loading."""
import json
import subprocess
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.native_embedding.acceptance import compare, reference_interpreter


class Decision(unittest.TestCase):
    def test_reference_venv_entry_keeps_torch_environment(self):
        interpreter = Path('.q4t-work/refenv/bin/python')
        result = subprocess.run([reference_interpreter(interpreter), '-c',
            'import json,sys,torch; print(json.dumps(dict(prefix=sys.prefix,'
            'torch=torch.__version__)))'], check=True, text=True,
            capture_output=True, timeout=30)
        value = json.loads(result.stdout)
        self.assertEqual(Path(value['prefix']).resolve(),
                         interpreter.parent.parent.resolve())
        self.assertEqual(value['torch'], '2.14.0+cpu')

    def fixture(self, root):
        root.mkdir()
        for kind in ['native', 'reference']:
            for index in range(14):
                output = root / kind / f'case-{index}'
                output.mkdir(parents=True)
                vector = np.zeros(1024, dtype='<f4')
                vector[index - 5 if index >= 9 else index] = 1
                (vector.view('<u4') >> 16).astype('<u2').tofile(output / 'last.bf16')
        return dict(cases=[{} for _ in range(14)], thresholds=dict(
            min_cosine=0.999, max_normalized_absolute_error=0.005,
            max_relative_l2_error=0.03, retrieval_top1_required=5))

    def test_exact_synthetic_reference_and_retrieval_pass(self):
        with tempfile.TemporaryDirectory(dir='.q4t-work') as temporary:
            root = Path(temporary) / 'exact'
            plan = self.fixture(root)
            compare(root, plan)
            result = json.loads((root / 'comparison.json').read_text())
            self.assertTrue(result['accepted'])
            self.assertEqual(result['top1_correct'], 5)

    def test_normalization_cannot_hide_wrong_hidden_magnitude(self):
        with tempfile.TemporaryDirectory(dir='.q4t-work') as temporary:
            root = Path(temporary) / 'scaled'
            plan = self.fixture(root)
            output = root / 'native/case-0/last.bf16'
            vector = np.zeros(1024, dtype='<f4')
            vector[0] = 2
            (vector.view('<u4') >> 16).astype('<u2').tofile(output)
            with self.assertRaises(AssertionError):
                compare(root, plan)
            result = json.loads((root / 'comparison.json').read_text())
            self.assertFalse(result['numerical_accepted'])
            self.assertEqual(result['top1_correct'], 5)

    def test_numerically_valid_but_wrong_retrieval_is_not_complete(self):
        with tempfile.TemporaryDirectory(dir='.q4t-work') as temporary:
            root = Path(temporary) / 'wrong-ranking'
            plan = self.fixture(root)
            for kind in ['native', 'reference']:
                query = root / kind / 'case-9/last.bf16'
                query.write_bytes((root / kind / 'case-5/last.bf16').read_bytes())
            with self.assertRaises(AssertionError):
                compare(root, plan)
            result = json.loads((root / 'comparison.json').read_text())
            self.assertTrue(result['numerical_accepted'])
            self.assertEqual(result['top1_correct'], 4)
            self.assertFalse(result['accepted'])


if __name__ == '__main__':
    unittest.main()
