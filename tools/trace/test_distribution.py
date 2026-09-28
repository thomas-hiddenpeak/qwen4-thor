"""Direct distribution contracts, including committed versus failed forwards."""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import tempfile
import unittest
import numpy as np
from distribution import distribution, histogram, overlap, read_request, top_set
from test_shadow import fixture

CHECKER = None
ROOT = Path(__file__).resolve().parents[2]


class Contracts(unittest.TestCase):
    def test_histogram_matches_independent_counter(self):
        ids = list(range(512))*10
        actual = histogram(struct.pack('<'+'H'*len(ids), *ids))
        self.assertEqual(actual.tolist(), [Counter(ids)[e] for e in range(512)])

    def test_zero_experts_and_mass(self):
        counts = histogram(struct.pack('<10H', *range(10)))
        self.assertEqual(len(counts), 512)
        self.assertEqual(int(counts.sum()), 10)
        self.assertEqual(distribution(counts)['active'], 10)
        self.assertAlmostEqual(distribution(counts)['top10'], 1)

    def test_uniform_entropy(self):
        result=distribution(np.ones(512))
        self.assertAlmostEqual(result['entropy_bits'], 9)
        self.assertAlmostEqual(result['effective_experts'], 512)
        self.assertAlmostEqual(result['top128'], .25)

    def test_empty_is_not_zero_entropy(self):
        self.assertIsNone(distribution(np.zeros(512))['entropy_bits'])
        self.assertIsNone(overlap(np.zeros(512),np.zeros(512)))

    def test_zero_ties_not_hotspots(self):
        a=np.zeros(512);a[3]=1
        self.assertEqual(top_set(a,64),{3})
        self.assertIsNone(overlap(a,a))

    def test_invalid_id(self):
        with self.assertRaises(ValueError):
            histogram(struct.pack('<10H', *([512]*10)))

    def read_fixture(self, failed):
        parent=ROOT/'build/distribution-contracts';parent.mkdir(exist_ok=True,parents=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            p=Path(tmp)/'source';m=fixture(p,outcome=3 if failed else 1,failed_forward=failed)
            (p/'request-1.partial').rename(p/'request-1.bin')
            return read_request(p/'request-1.bin',m,CHECKER,[0])

    def test_stage_counts_and_halves(self):
        _,checked,_,_,count,blocks,halves,groups,_=self.read_fixture(False)
        self.assertEqual(count.sum(),960)
        self.assertEqual(groups,[1,1])
        self.assertTrue(np.array_equal(count,halves.sum(axis=1)))
        self.assertTrue(np.array_equal(count,blocks))
        self.assertEqual(checked['successful_requests'],1)

    def test_failed_forward_excluded(self):
        _,checked,_,_,count,blocks,halves,groups,_=self.read_fixture(True)
        self.assertEqual(count[0].sum(),480)
        self.assertEqual(count[1].sum(),0)
        self.assertEqual(groups,[1,0])
        self.assertEqual(checked['failed_requests'],1)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--checker',type=Path,required=True)
    args,selected=ap.parse_known_args();CHECKER=args.checker.resolve()
    unittest.main(argv=[__file__]+selected)
