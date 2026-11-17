"""Retained real JSON/SSE with offline service checks; no new GPU evidence."""
import copy
import json
from pathlib import Path
import unittest

from tools.native_asr import auxiliary_service as audio
from tools.native_asr import service_acceptance as service

ROOT = Path(__file__).resolve().parents[2]
DIRECT = ROOT/'docs/evidence/serve-speaker-http-20261118'
PROFILES = ROOT/'docs/evidence/serve-speaker-profiles-20261118'


class SpeakerService(unittest.TestCase):
    def setUp(self):
        self.identity = json.loads((PROFILES/'qualified/registry.json').read_text())['identity']
        self.config = json.loads((PROFILES/'primary-speaker-config.wire').read_text())
        self.response = json.loads((PROFILES/'primary-speaker.wire').read_text())

    def test_actual_retained_voice_labels_source_and_each_dependency_health(self):
        result = audio.speaker_result(self.response, self.identity, self.config,
                                      audio.evidence.SPEAKER_FILE_SHA)
        self.assertEqual(result['labels'], ['A','B','A'])
        self.assertEqual(result['auxiliaries'], ['alignment','vad','speaker'])
        for name in ('alignment','vad','speaker'):
            for field, bad in [('state','configured'),('core_readiness_dependency',True),
                               ('compute_failures',False), ('model','other')]:
                config = copy.deepcopy(self.config);config['native_'+name+'_health'][field] = bad
                with self.assertRaises(ValueError):
                    audio.speaker_result(self.response,self.identity,config,audio.evidence.SPEAKER_FILE_SHA)
        with self.assertRaises(ValueError):
            audio.speaker_result(self.response,self.identity,self.config,'0'*64)
        with self.assertRaises(ValueError): audio.source(b'not qualified speech',self.identity)

    def test_real_sse_and_multipart_preserve_every_segment_and_stream_identity(self):
        wire = (DIRECT/'js-stream.wire').read_bytes()
        response = service.stream_value(wire)
        result = audio.speaker_result(response,self.identity,self.config,
                                     audio.evidence.SPEAKER_FILE_SHA,stream=True)
        self.assertEqual(result['labels'],['A','B','A'])
        self.assertTrue(result['stream'])
        before = copy.deepcopy(self.identity)
        request = audio.request(self.identity,stream=True)
        self.assertEqual(request['response_format'],'diarized_json')
        self.assertNotIn('include',request)
        fields = dict(audio.fields(request))
        self.assertEqual(fields['stream'],'true')
        self.assertEqual(json.loads(fields['chunking_strategy']),request['chunking_strategy'])
        self.assertEqual(audio.selected(self.identity,True),before)
        events = [json.loads(line[6:]) for line in wire.decode().splitlines() if line.startswith('data: {')]
        faults = [lambda v:v.insert(0,v[-1]),
            lambda v:v[0].update(segment_id='wrong'),
            lambda v:v[1].update(speaker='wrong'),
            lambda v:v[-1]['segments'].pop(),
            lambda v:v[-1].update(text='fake')]
        for mutate in faults:
            bad = copy.deepcopy(events);mutate(bad)
            raw=''.join('data: '+json.dumps(e)+'\n\n' for e in bad).encode()
            with self.assertRaises(RuntimeError): service.stream_value(raw)
        with self.assertRaises(RuntimeError):service.stream_value(wire+b'data: [DONE]\n\n')

    def test_budget_and_output_loss_cannot_pass_whole_payload_witness(self):
        for key in self.identity['native_asr']['speaker']['limits']:
            config=copy.deepcopy(self.config);config['native_speaker_limits'][key]+=1
            with self.assertRaises(ValueError):
                audio.speaker_result(self.response,self.identity,config,audio.evidence.SPEAKER_FILE_SHA)
        for key in ('native_alignment_limits','native_vad_max_chunks'):
            config=copy.deepcopy(self.config)
            if key=='native_alignment_limits':config[key]['max_words']+=1
            else:config[key]+=1
            with self.assertRaises(ValueError):
                audio.speaker_result(self.response,self.identity,config,audio.evidence.SPEAKER_FILE_SHA)
        for mutate in [lambda v:v['segments'][2].update(speaker='C'),
                       lambda v:v['segments'].pop(),
                       lambda v:v['usage'].update(total_tokens=0)]:
            response=copy.deepcopy(self.response);mutate(response)
            with self.assertRaises(ValueError):
                audio.speaker_result(response,self.identity,self.config,audio.evidence.SPEAKER_FILE_SHA)


if __name__ == '__main__':
    unittest.main()
