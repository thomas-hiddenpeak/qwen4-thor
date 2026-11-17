"""Real overlap gates reject silent source loss, fabricated voices and SSE drift."""
import copy
import unittest

from tools.native_separator.http_acceptance import EXPECTED, check_result, check_stream


def fixture():
    clip = dict(samples=64000, utterances=[
        dict(begin_sample=3200, end_sample=49120, voice='0', expected_text=EXPECTED[0]),
        dict(begin_sample=3200, end_sample=35360, voice='1', expected_text=EXPECTED[1])])
    segments = [dict(id=str(i), type='transcript.text.segment', speaker=speaker,
        start=start, end=end, text=EXPECTED[i])
        for i, (speaker, start, end) in enumerate([('A', .3, 3.), ('B', .35, 2.1)])]
    value = dict(task='transcribe', duration=4, text=' '.join(EXPECTED), segments=segments,
        usage=dict(type='tokens', input_tokens=120, output_tokens=16, total_tokens=136,
                   input_token_details=dict(audio_tokens=80, text_tokens=40)))
    return clip, value


class OverlapAcceptance(unittest.TestCase):
    def test_both_real_voices_overlap_and_reference_mapping(self):
        clip, value = fixture()
        self.assertGreater(check_result(value, clip)['overlap_seconds'], 1.)
        renamed = copy.deepcopy(value)
        renamed['segments'][0]['speaker'] = 'second'
        renamed['segments'][1]['speaker'] = 'first'
        self.assertEqual(check_result(renamed, clip, {'0':'second', '1':'first'})[
            'voice_labels'], ['second', 'first'])
        for defect in ['merged', 'dropped', 'lost_words', 'serialized', 'order', 'nonfinite', 'text']:
            bad = copy.deepcopy(value)
            if defect == 'merged': bad['segments'][1]['speaker'] = 'A'
            if defect == 'dropped': bad['segments'].pop()
            if defect == 'lost_words': bad['segments'][1]['text'] = 'only one voice'
            if defect == 'serialized': bad['segments'][1].update(start=3., end=3.5)
            if defect == 'order': bad['segments'][1]['start'] = .1
            if defect == 'nonfinite': bad['segments'][1]['end'] = float('nan')
            if defect == 'text': bad['text'] = 'wrong final text'
            with self.assertRaises(Exception, msg=defect): check_result(bad, clip)

    def test_quality_observation_does_not_hide_protocol_errors(self):
        clip, value = fixture()
        value['segments'][0]['text'] = EXPECTED[0].replace('hurt', 'hurts')
        value['text'] = ' '.join(row['text'] for row in value['segments'])
        with self.assertRaises(Exception): check_result(value, clip)
        observed = check_result(value, clip, enforce_quality=False)
        self.assertFalse(observed['quality_passed'])
        self.assertEqual(observed['per_voice_word_error_rate'][0], 1/7)
        value['segments'][1]['start'] = 3.2
        with self.assertRaises(Exception): check_result(value, clip, enforce_quality=False)

    def test_single_silence_and_cumulative_usage(self):
        clip, value = fixture()
        clip['utterances'] = clip['utterances'][:1]
        value['segments'] = value['segments'][:1]; value['text'] = EXPECTED[0]
        self.assertEqual(check_result(value, clip)['voice_labels'], ['A'])
        duplicate = copy.deepcopy(value)
        duplicate['segments'].append(dict(value['segments'][0], id='1', speaker='B'))
        with self.assertRaises(Exception): check_result(duplicate, clip)
        for defect in ['negative', 'sum', 'details']:
            bad = copy.deepcopy(value)
            if defect == 'negative': bad['usage']['output_tokens'] = -1
            if defect == 'sum': bad['usage']['total_tokens'] += 1
            if defect == 'details': bad['usage']['input_token_details']['audio_tokens'] = 120
            with self.assertRaises(Exception): check_result(bad, clip)
        clip['utterances'] = []
        value.update(text='', segments=[], usage=dict(type='tokens', input_tokens=0,
            output_tokens=0, total_tokens=0, input_token_details=dict(audio_tokens=0, text_tokens=0)))
        self.assertTrue(check_result(value, clip)['silence'])
        value['usage']['output_tokens'] = 1
        with self.assertRaises(Exception): check_result(value, clip)

    def test_stream_keeps_committed_text_ids_and_original_overlap(self):
        clip, value = fixture(); events = []
        for i, row in enumerate(value['segments']):
            events += [dict(type='transcript.text.delta', segment_id=row['id'],
                            delta=(' ' if i else '') + row['text']), copy.deepcopy(row)]
        events.append(dict(copy.deepcopy(value), type='transcript.text.done'))
        self.assertEqual(check_stream(dict(events=events), clip)['voice_labels'], ['A', 'B'])
        for defect in ['id', 'drop', 'done', 'changed']:
            bad = copy.deepcopy(events)
            if defect == 'id': bad[0]['segment_id'] = '9'
            if defect == 'drop': del bad[3]
            if defect == 'done': bad.insert(1, copy.deepcopy(bad[-1]))
            if defect == 'changed': bad[1]['end'] = 1.9
            with self.assertRaises(Exception): check_stream(dict(events=bad), clip)


if __name__ == '__main__': unittest.main()
