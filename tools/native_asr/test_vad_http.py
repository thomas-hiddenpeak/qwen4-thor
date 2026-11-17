"""Small synthetic positive/negative controls for the VAD business oracle."""
import copy
import unittest
import zlib

from tools.native_asr import vad_http_acceptance as vad


def segment_fixture():
    text = vad.asr.EXPECTED['zh']
    visible = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    mapping = {value: chr(value) for value in visible}
    for count, value in enumerate(v for v in range(256) if v not in visible):
        mapping[value] = chr(256 + count)
    vocab = {''.join(mapping[b] for b in text.encode()): 42}
    clip = dict(duration_ms=4000, utterances=[dict(begin_sample=0, end_sample=16000),
                                           dict(begin_sample=32000, end_sample=48000)])
    rows, words = [], []
    for i in range(2):
        rows.append(dict(id=i, seek=i*200, start=i*2+.08, end=i*2+.8, text=text,
                         tokens=[42], temperature=0, avg_logprob=-.25,
                         compression_ratio=len(text.encode())/len(zlib.compress(text.encode(), 6)),
                         no_speech_prob=.01))
        for j in range(13):
            words.append(dict(word=text[j], start=(i*2000+80+720*j//13)/1000,
                              end=(i*2000+80+720*(j+1)//13)/1000))
    return dict(text=text+' '+text, language='Chinese', duration=4,
                usage=dict(type='duration', seconds=4), segments=rows, words=words), clip, vocab


class VadHttpOracle(unittest.TestCase):
    def test_combined_actual_offsets_and_negative_controls(self):
        value, clip, vocab = segment_fixture()
        self.assertEqual(vad.check_combined(value, clip, vocab)['words'], 26)
        faults = [lambda v: v['segments'][1].update(seek=0),
                  lambda v: v['segments'][1].update(id=0),
                  lambda v: v['segments'][1].update(start=.1),
                  lambda v: v['segments'][1].update(no_speech_prob=float('nan')),
                  lambda v: v['segments'][1].update(compression_ratio=1),
                  lambda v: v['words'][-1].update(end=20),
                  lambda v: v.update(duration=2),
                  lambda v: v['segments'].pop()]
        for mutate in faults:
            broken = copy.deepcopy(value); mutate(broken)
            with self.assertRaises(RuntimeError): vad.check_combined(broken, clip, vocab)

    def test_stream_requires_every_separator_probability_and_one_terminal(self):
        text = vad.asr.EXPECTED['zh']
        logs = [dict(token=text, logprob=-.25, bytes=list(text.encode()))]
        usage = dict(type='tokens', input_tokens=140, output_tokens=22, total_tokens=162,
                     input_token_details=dict(audio_tokens=110, text_tokens=30))
        events = [dict(type='transcript.text.delta', delta=text, logprobs=logs),
                  dict(type='transcript.text.delta', delta=' '+text, logprobs=logs),
                  dict(type='transcript.text.done', text=text+' '+text, logprobs=logs*2, usage=usage)]
        self.assertEqual(vad.check_stream(dict(events=events), b'wire')['events'], 3)
        for fault in range(4):
            broken = copy.deepcopy(events)
            if fault == 0: broken[1]['delta'] = text
            if fault == 1: broken[-1]['logprobs'].pop()
            if fault == 2: broken[-1]['usage'].update(output_tokens=25, total_tokens=165)
            if fault == 3: broken.append(copy.deepcopy(broken[-1]))
            with self.assertRaises(RuntimeError): vad.check_stream(dict(events=broken), b'wire')
        with self.assertRaises(RuntimeError): vad.check_stream(dict(events=events), b'[DONE]')

    def test_silence_rejects_fabricated_model_work_or_text(self):
        value = dict(text='', usage=dict(type='tokens', input_tokens=0, output_tokens=0,
                     total_tokens=0, input_token_details=dict(audio_tokens=0, text_tokens=0)))
        vad.check_empty(value)
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            broken = copy.deepcopy(value); broken['usage'][field] = 1
            with self.assertRaises(RuntimeError): vad.check_empty(broken)
        broken = copy.deepcopy(value); broken['text'] = 'invented'
        with self.assertRaises(RuntimeError): vad.check_empty(broken)


if __name__ == '__main__':
    unittest.main()
