"""Negative controls for actual-record accounting, no model or GPU."""
import unittest

from tools.vision_service.closeout_audit import final_counts,json_content


class Audit(unittest.TestCase):
    def test_bad_count_does_not_pass_on_an_unrelated_number(self):
        text='- starts: 7\n| starts | 6 | 6 | ✅ Yes |\n[cite]'
        with self.assertRaisesRegex(RuntimeError,'incorrect final count'):
            final_counts(text,dict(starts=6),'[cite]')

    def test_image_disagreement_and_fabricated_citation_are_rejected(self):
        text='- starts: 6\n| starts | 5 | 6 | ✅ Yes |\n[cite]'
        with self.assertRaisesRegex(RuntimeError,'incorrect image/source'):
            final_counts(text,dict(starts=6),'[cite]')
        with self.assertRaisesRegex(RuntimeError,'citation missing'):
            final_counts(text,dict(starts=6),'[actual]')

    def test_free_output_fences_do_not_hide_extra_prose(self):
        self.assertEqual(json_content('```json\n{"direction":"right"}\n```'),dict(direction='right'))
        with self.assertRaises(ValueError):json_content('anything ```json\n{}\n```')


if __name__=='__main__':unittest.main()
