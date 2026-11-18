"""Targeted false-acceptance and damaged-audio contracts; no model load."""
from array import array
import unittest

try:
    from .clock_content import check_content, damage_pcm
except ImportError:
    from clock_content import check_content, damage_pcm

EN = dict(text='Hello, this is a native speech test.', language='english')
CN = dict(text='你好，会议下午三点开始。', language='chinese')
RECEIPT = dict(eos=True, generated_tokens=13)


class ClockContentTests(unittest.TestCase):
    def test_silence_removes_content_without_mutating_original(self):
        source = array('f', [0.25, -0.5]) * 501
        saved = source.tobytes()
        changed = damage_pcm(source, 'silence')
        self.assertEqual(len(changed), len(source))
        self.assertFalse(any(changed))
        self.assertEqual(source.tobytes(), saved)

    def test_quarter_is_exact_prefix_and_zero_tail(self):
        source = array('f', [0.25, -0.5]) * 501
        saved = source.tobytes()
        changed = damage_pcm(source, 'first_quarter')
        self.assertEqual(changed[:250], source[:250])
        self.assertEqual(len(changed), len(source))
        self.assertFalse(any(changed[250:]))
        self.assertEqual(source.tobytes(), saved)

    def test_invalid_audio_or_unplanned_mutation_rejected(self):
        for source in ([], array('d', [0.0]) * 640, array('f', [0.0]) * 639,
                       array('f', [0.0]) * 120001, array('f', [float('nan')]) * 640,
                       array('f', [float('inf')]) * 640, array('f', [1.01]) * 640):
            with self.assertRaises(ValueError):
                damage_pcm(source, 'silence')
        with self.assertRaises(ValueError):
            damage_pcm(array('f', [0.0]) * 640, 'random_search')

    def test_exact_english_and_chinese_pass_content_only(self):
        for case, text in ((EN, 'Hello. This is a native speech test.'),
                           (CN, '你好，会议下午三点开始。')):
            value = check_content('language ' + case['language'].capitalize() +
                                  '<asr_text>' + text, RECEIPT, case)
            self.assertTrue(value['passed'])
            self.assertFalse(value['listening_qualified'])

    def test_missing_sentence_is_not_positive(self):
        for case, prefix in ((EN, 'Hello.'), (CN, '你好。')):
            text = 'language ' + case['language'].capitalize() + '<asr_text>' + prefix
            self.assertFalse(check_content(text, RECEIPT, case)['passed'])
            self.assertTrue(check_content(text, RECEIPT, case, negative=True)['passed'])

    def test_full_sentence_in_negative_is_rejected_even_wrong_language(self):
        for case in (EN, CN):
            for language in ('English', 'Chinese', 'None'):
                text = 'language ' + language + '<asr_text>' + case['text']
                self.assertFalse(check_content(text, RECEIPT, case,
                                               negative=True)['passed'])

    def test_incomplete_or_invalid_execution_cannot_pass_negative(self):
        for receipt in (dict(eos=False, generated_tokens=13),
                        dict(eos=1, generated_tokens=13),
                        dict(eos=True, generated_tokens=True),
                        dict(eos=True, generated_tokens=0),
                        dict(eos=True, generated_tokens=65), {}):
            with self.assertRaises(ValueError):
                check_content('', receipt, EN, negative=True)

    def test_invalid_protocol_rejected_and_empty_content_never_positive(self):
        for text in ('Hello.', 'language English', '<asr_text>Hello',
                     'language English<asr_text><asr_text>Hello',
                     'language English<asr_text><tool>'):
            with self.assertRaises(ValueError):
                check_content(text, RECEIPT, EN, negative=True)
        for text in ('', 'language None<asr_text>'):
            self.assertTrue(check_content(text, RECEIPT, EN, negative=True)['passed'])
            self.assertFalse(check_content(text, RECEIPT, EN)['passed'])


if __name__ == '__main__':
    unittest.main()
