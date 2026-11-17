"""CPU negative controls for the real segment business acceptance oracle."""
import copy
import unittest
import zlib

from tools.native_asr.segment_http_acceptance import check_segment


class SegmentOracle(unittest.TestCase):
    def value(self):
        text = 'Hello world'
        return dict(text=text, duration=1.2, language='English',
            usage=dict(type='duration', seconds=1.2),
            segments=[dict(id=0, seek=0, start=.16, end=.96, text=text,
                tokens=[8, 9], temperature=.4, avg_logprob=-.5,
                no_speech_prob=.001,
                compression_ratio=len(text) / len(zlib.compress(text.encode())))])

    def test_actual_fields_and_optional_words(self):
        value = self.value()
        self.assertEqual(check_segment(value, 1200)['tokens'], 2)
        value['words'] = [dict(word='Hello', start=.16, end=.4),
                          dict(word='world', start=.48, end=.96)]
        check_segment(value, 1200, include_words=True)

    def test_missing_or_fabricated_statistics_fail(self):
        for key, wrong in [('start', -1), ('end', 2), ('avg_logprob', float('nan')),
                           ('temperature', 2), ('no_speech_prob', -1),
                           ('tokens', []), ('tokens', [151936]),
                           ('compression_ratio', 1), ('seek', 1), ('text', 'other')]:
            value = copy.deepcopy(self.value())
            value['segments'][0][key] = wrong
            with self.subTest(key=key, value=wrong), self.assertRaises(RuntimeError):
                check_segment(value, 1200)

    def test_silence_has_no_invented_segment_or_text(self):
        value = self.value()
        value['segments'] = []
        with self.assertRaises(RuntimeError):
            check_segment(value, 1200, empty=True)
        value['text'] = ''
        self.assertTrue(check_segment(value, 1200, empty=True)['empty'])


if __name__ == '__main__':
    unittest.main()
