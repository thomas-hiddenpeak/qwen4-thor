"""Negative controls for the bounded real-voice business acceptance checker."""
import copy
import unittest

from tools.native_speaker.http_acceptance import check_result, check_stream


def fixture():
    clip = dict(samples=128000, utterances=[
        dict(begin_sample=0,end_sample=32000,voice='A'),
        dict(begin_sample=48000,end_sample=80000,voice='B'),
        dict(begin_sample=96000,end_sample=128000,voice='A')])
    rows = [dict(id=str(i),type='transcript.text.segment',speaker=u['voice'],
        start=u['begin_sample']/16000+.1,end=u['end_sample']/16000-.1,text=t)
        for i,(u,t) in enumerate(zip(clip['utterances'],['one','two','three']))]
    value = dict(task='transcribe',duration=8,text='one two three',segments=rows,
        usage=dict(type='tokens',input_tokens=40,output_tokens=10,total_tokens=50,
                   input_token_details=dict(audio_tokens=30,text_tokens=10)))
    return clip, value


class DiarizationAcceptance(unittest.TestCase):
    def test_real_interval_labels_return_and_missing_utterance(self):
        clip,value = fixture()
        self.assertEqual(check_result(value,clip)['labels'],['A','B','A'])
        known = copy.deepcopy(value)
        for row in known['segments']:row['speaker'] = {'A':'first','B':'second'}[row['speaker']]
        self.assertEqual(check_result(known,clip,dict(A='first',B='second'))['covered_utterances'],3)
        for defect in ('merged','return','missing','timing','nonfinite','text'):
            bad = copy.deepcopy(value)
            if defect == 'merged':bad['segments'][1]['speaker'] = 'A'
            if defect == 'return':bad['segments'][2]['speaker'] = 'C'
            if defect == 'missing':bad['segments'].pop()
            if defect == 'timing':bad['segments'][1]['end'] = 6.5
            if defect == 'nonfinite':bad['segments'][1]['start'] = float('nan')
            if defect == 'text':bad['text'] = 'lost content'
            with self.assertRaises(Exception):check_result(bad,clip)

    def test_sse_terminal_delta_linkage_and_final_segments(self):
        clip,value = fixture();events=[]
        for i,row in enumerate(value['segments']):
            events += [dict(type='transcript.text.delta',segment_id=row['id'],
                            delta=(' ' if i else '')+row['text']),copy.deepcopy(row)]
        events.append(dict(copy.deepcopy(value),type='transcript.text.done'))
        self.assertEqual(check_stream(dict(events=events),clip)['labels'],['A','B','A'])
        for defect in ('id','drop','done','final'):
            bad = copy.deepcopy(events)
            if defect == 'id':bad[0]['segment_id'] = '999'
            if defect == 'drop':del bad[3]
            if defect == 'done':bad.insert(1,copy.deepcopy(bad[-1]))
            if defect == 'final':bad[1]['text'] = 'wrong'
            with self.assertRaises(Exception):check_stream(dict(events=bad),clip)

    def test_usage_and_silence_cannot_fabricate_success(self):
        clip,value = fixture()
        for defect in ('negative','sum','detail'):
            bad = copy.deepcopy(value)
            if defect == 'negative':bad['usage']['output_tokens'] = -1
            if defect == 'sum':bad['usage']['total_tokens'] = 99
            if defect == 'detail':bad['usage']['input_token_details']['audio_tokens'] = 31
            with self.assertRaises(Exception):check_result(bad,clip)
        silence = dict(value,text='',segments=[],duration=2,
            usage=dict(type='tokens',input_tokens=0,output_tokens=0,total_tokens=0,
                       input_token_details=dict(audio_tokens=0,text_tokens=0)))
        quiet = dict(samples=32000,utterances=[])
        self.assertTrue(check_result(silence,quiet)['silence'])
        silence['usage']['output_tokens'] = 1
        with self.assertRaises(Exception):check_result(silence,quiet)


if __name__ == '__main__':unittest.main()
