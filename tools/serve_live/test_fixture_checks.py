"""The fixture proof must not hide extra inference, missing audio or false drain."""
import base64
import copy
import hashlib
import unittest

from tools.serve_live.fixture_checks import observe
from tools.serve_live.test_checks import fixture as conversation_fixture


def fixture():
    receipt,journal,before,after,record = conversation_fixture()
    pcm = bytes([1,2])*480
    clip = dict(bytes=len(pcm),sha256=hashlib.sha256(pcm).hexdigest())
    playing = dict(type='session.output_audio.fixture.playing',event_id='fixture_begin',
                   client_event_id='fixture_audio')
    played = dict(type='session.output_audio.fixture.played',event_id='fixture_end',
                  client_event_id='fixture_audio')
    output = dict(type='session.output_audio.delta',delta=base64.b64encode(pcm).decode())
    def row(direction,event):return dict(direction=direction,event=event)
    expanded = []
    for original in journal:
        kind = original['event']['type']
        if kind == 'session.input_audio.muted':
            expanded += [row('client',dict(type='session.input_audio.mute',event_id='mute')),
                         original,row('server',played),
                         row('client',dict(type='session.input_audio.append',event_id='fixture_muted',
                             audio=base64.b64encode(pcm+bytes(48000)).decode())),
                         row('client',dict(type='session.input_audio.unmute',event_id='unmute'))]
        else:expanded.append(original)
        if kind == 'session.started':
            expanded += [row('client',dict(type='session.output_audio.fixture.play',event_id='fixture_audio',
                                          audio=base64.b64encode(pcm).decode())),
                         row('server',playing),row('server',output),
                         row('client',dict(type='session.input_audio.append',event_id='fixture_drop',
                             audio=base64.b64encode(pcm+bytes(48000)).decode()))]
    for i,item in enumerate(expanded):item['monotonic_ns']=str(i*10000000)
    receipt['events']=[r['event'] for r in expanded if r['direction']=='server']
    receipt['fixture']=dict(clip,played=True,listener_heard=False)
    return [receipt,expanded,before,after,record,clip]


class FixtureChecks(unittest.TestCase):
    def reject(self,mutate):
        args=fixture();mutate(args)
        with self.assertRaises((RuntimeError,ValueError,KeyError)):
            observe(*args)

    def test_exact_fixture_and_conversation_have_separate_receipts(self):
        args=fixture();original=copy.deepcopy(args)
        result=observe(*args)
        self.assertEqual(args,original)
        self.assertEqual(result['fixture']['bytes'],960)
        self.assertEqual(result['waveform_bytes'],4800)
        self.assertEqual(result['admitted'],3)
        self.assertEqual(result['fixture']['new_model_work'],0)

    def test_wrong_or_missing_fixture_audio_cannot_pass(self):
        for value in ('',base64.b64encode(bytes(960)).decode()):
            self.reject(lambda args:args[0]['events'][2].update(delta=value))

    def test_unconfirmed_drain_cannot_claim_played(self):
        self.reject(lambda args:args[0]['fixture'].update(played=False))
        self.reject(lambda args:args[0]['fixture'].update(listener_heard=True))
        self.reject(lambda args:args[0]['events'][4].update(client_event_id='foreign'))

    def test_inference_or_extra_quota_is_not_hidden_by_fixture_view(self):
        self.reject(lambda args:args[0]['events'][2].update(type='session.input_transcript.delta'))
        self.reject(lambda args:args[3].update({'q4t_client_rate_admitted_total{client="alpha"}':4}))

    def test_input_probes_must_be_in_actual_playback_and_mute_intervals(self):
        def late(args):
            probe=next(r for r in args[1] if r['event'].get('event_id')=='fixture_drop')
            args[1].remove(probe);args[1].append(probe)
        self.reject(late)
        def no_voice(args):
            probe=next(r for r in args[1] if r['event'].get('event_id')=='fixture_muted')
            probe['event']['audio']=base64.b64encode(bytes(48960)).decode()
        self.reject(no_voice)


if __name__=='__main__':unittest.main()
