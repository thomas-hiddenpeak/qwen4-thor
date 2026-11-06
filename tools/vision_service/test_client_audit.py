"""Negative controls: all expected numbers present is not sufficient."""
import copy
import unittest

from tools.vision_service.client_audit import final_truth


class Audit(unittest.TestCase):
    def setUp(self):
        self.record=dict(totals=dict(starts=6,generations=70,controls=67,native=5))
        self.source=dict(citation="[docs/record.json:L1-L12]")
        self.actual=dict(counts=copy.deepcopy(self.record["totals"]),matches_image=True,
                         citation=self.source["citation"])

    def test_exact_source_and_comparison(self):
        final_truth(self.actual,self.source,self.record)

    def test_swapped_numbers_rejected(self):
        self.actual["counts"]["generations"]=67
        self.actual["counts"]["controls"]=70
        with self.assertRaises(RuntimeError):final_truth(self.actual,self.source,self.record)

    def test_unread_citation_or_false_comparison_rejected(self):
        for key,value in (("citation","[docs/record.json:L1-L99]"),("matches_image",False)):
            changed=dict(self.actual,**{key:value})
            with self.assertRaises(RuntimeError):final_truth(changed,self.source,self.record)


if __name__=="__main__":unittest.main()
