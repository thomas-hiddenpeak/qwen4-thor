"""Independent prefix/counter/source counterexamples; no model quality waiver."""
import copy
import unittest
from unittest import mock
from tools.serve_recovery import ledger, profile_record
from tools.serve_recovery import profile_duplex_composed as c
from tools.serve_recovery import profile_duplex_composed_reuse as r
from tools.serve_recovery.test_profile_duplex import capabilities


def vad():
    def event(kind, **data):
        return dict(type=kind, **data)
    events = [event('input_audio_buffer.speech_started', item_id='spoken'),
              event('input_audio_buffer.committed', item_id='spoken')]
    responses = []
    for i, text in enumerate((c.d.PHRASE, 'READY', '1')):
        identity = 'r'+str(i)
        usage = dict(input_tokens=4, output_tokens=3, total_tokens=7)
        response = dict(id=identity, status='completed', usage=usage,
            output_modalities=['text'], output=[dict(type='message',
            content=[dict(type='output_text', text=text)])])
        if i == 0:
            events += [event('conversation.item.input_audio_transcription.completed',
                item_id='spoken', content_index=0, transcript=text, usage=usage)]
        if i == 1:
            events += [event('input_audio_buffer.timeout_triggered',
                             item_id='silent', audio_start_ms=10, audio_end_ms=10),
                       event('input_audio_buffer.committed', item_id='silent')]
        events += [event('response.created', response=dict(id=identity)),
                   event('response.output_text.delta', response_id=identity, delta=text),
                   event('response.done', response=response)]
        if i < 2:
            responses.append(dict(label=('vad_auto', 'idle')[i], response=response))
    events.append(event('input_audio_buffer.speech_started', item_id='too_late'))
    for i, item in enumerate(events):
        item['event_id'] = 'event-'+str(i)
    return dict(status='FAIL', error='response status: completed', connections=1,
        automatic_replay=False, response_creates=1, close_code=None, events=events,
        responses=responses, quality=[c.c.judgment('vad_auto', c.d.PHRASE, c.d.PHRASE),
                                     c.c.judgment('idle', 'READY', 'READY')])


class Prefix(unittest.TestCase):
    def test_completed_prefix_does_not_rewrite_failed_session(self):
        value = vad()
        original = copy.deepcopy(value)
        c.original_vad(value)
        self.assertEqual(value, original)
        self.assertEqual(value['status'], 'FAIL')

    def test_missing_terminal_or_failed_input_cannot_qualify_prefix(self):
        for kind in ('response.done', 'conversation.item.input_audio_transcription.completed'):
            value = vad()
            del value['events'][next(i for i,e in enumerate(value['events'])
                                     if e['type'] == kind)]
            with self.assertRaises((ValueError, RuntimeError)):
                c.original_vad(value)

    def test_late_interrupt_failure_cannot_be_relabelled_cancelled(self):
        for mutate in (lambda v:v.update(status='PASS'),
                       lambda v:v.update(error='different failure'),
                       lambda v:v['events'][-2]['response'].update(status='cancelled')):
            value = vad()
            mutate(value)
            with self.assertRaises(ValueError):
                c.original_vad(value)

    def test_idle_samples_or_fabricated_transcription_are_rejected(self):
        for add in (False, True):
            value = vad()
            if add:
                row = copy.deepcopy(value['events'][2])
                row.update(event_id='fake', item_id='silent')
                value['events'].insert(3, row)
            else:
                next(e for e in value['events'] if e['type'] ==
                     'input_audio_buffer.timeout_triggered')['audio_end_ms'] = 11
            with self.assertRaises(ValueError):
                c.original_vad(value)

    def test_model_failure_cannot_be_hidden_by_a_pass_label(self):
        value = vad()
        response = value['responses'][0]['response']
        response['output'][0]['content'][0]['text'] = 'wrong'
        next(e for e in value['events'] if e['type'] ==
             'response.output_text.delta')['delta'] = 'wrong'
        with self.assertRaises(ValueError):
            c.original_vad(value)

    def test_exact_settlement_and_real_private_residual(self):
        zero = dict(admitted=0, settled=0, reserved=0, active=0,
                    sessions=0, host_bytes=0, free_slots=1)
        witness = dict(before=zero, after=dict(zero, admitted=2, settled=20))
        observed = dict(admitted=2, visible_tokens=20, private_asr=0)
        c.settled(witness, observed)
        for changes in (dict(settled=21), dict(admitted=3), dict(host_bytes=1)):
            bad = copy.deepcopy(witness)
            bad['after'].update(changes)
            with self.assertRaises(ValueError):
                c.settled(bad, observed)
        with self.assertRaises(ValueError):
            c.settled(witness, dict(observed, private_asr=1))
        c.settled(dict(witness, after=dict(witness['after'], settled=21)),
                  dict(observed, private_asr=1))


class Source(unittest.TestCase):
    def setUp(self):
        old = dict(identity=dict(binary_sha256=r.prior.REVIEW['previous_binary'],
            protocol_sha256=r.prior.REVIEW['previous_protocol'], model='fixed'),
            capacity=dict(max_tokens=256), capabilities=dict(capabilities(modern=False),
            protocol_source_sha256=r.prior.REVIEW['previous_protocol']),
            business=dict(schema=dict(answer=42), realtime={}, semantic_vad={}))
        source = dict(identity=ledger.digest(old['identity']),
            capacity=ledger.digest(old['capacity']),
            capabilities=ledger.digest(old['capabilities']),
            business={k:ledger.digest(v) for k,v in old['business'].items()})
        self.value = copy.deepcopy(old)
        self.value.update(format='q4t-profile-business-v20', profile='primary')
        self.value['identity'].update(binary_sha256=r.REVIEW['candidate_binary'],
                                     protocol_sha256=r.REVIEW['candidate_protocol'])
        self.value['capabilities'] = dict(capabilities(),
            protocol_source_sha256=r.REVIEW['candidate_protocol'])
        self.value['business']['realtime_duplex'] = dict(format=c.FORMAT,
            profile='primary', source_review=copy.deepcopy(c.REVIEW), data={'actual': 'fixed'})
        observed = dict(input_digests={k:ledger.digest({}) for k in r.INPUTS},
                        duplex_digest=ledger.digest({'actual': 'fixed'}))
        for module, name, value in ((r.prior, 'SOURCES', dict(primary=source)),
                (r, 'OBSERVED', dict(primary=observed)),
                (c, 'OBSERVED', dict(primary=observed))):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(c.d, 'original_business')
        patch.start()
        self.addCleanup(patch.stop)

    def test_exact_sources_are_reviewed_not_claimed_fresh(self):
        value = r.attach(self.value)
        r.validate(value)
        self.assertEqual(value['reused_business']['reviewed_input_keys'], list(r.INPUTS))
        self.assertNotIn('fresh_keys', value['reused_business'])
        self.assertFalse(value['reused_business']['review']['model_quality_repaired'])

    def test_wrong_binary_backend_capacity_or_capability_is_rejected(self):
        for mutate in (lambda v:v['identity'].update(binary_sha256='0'*64),
            lambda v:v['identity'].update(model='other'),
            lambda v:v['capacity'].update(max_tokens=1),
            lambda v:v['capabilities'].update(extra=True)):
            value = r.attach(self.value)
            mutate(value)
            with self.assertRaises(ValueError):
                r.validate(value)

    def test_changed_original_or_input_leaf_cannot_be_reused(self):
        for key in ('schema', 'realtime', 'semantic_vad'):
            value = r.attach(self.value)
            value['business'][key] = 'changed'
            with self.assertRaises(ValueError):
                r.validate(value)
        value = r.attach(self.value)
        value['business']['realtime_duplex']['data'] = {'actual': 'replacement'}
        with self.assertRaises(ValueError):
            r.validate(value)

    def test_wrong_review_and_profile_owner_are_rejected(self):
        for mutate in (lambda v:v['reused_business']['review'].update(model_quality_repaired=True),
            lambda v:v['business']['realtime_duplex'].update(profile='light'),
            lambda v:v['reused_business'].update(reviewed_input_keys=[])):
            value = r.attach(self.value)
            mutate(value)
            with self.assertRaises(ValueError):
                r.validate(value)

    def test_larger_record_is_only_allowed_with_reviewed_v20_identity(self):
        value = r.attach(self.value)
        profile_record.shape(value, (8 << 20))
        for bad, size in ((value, (8 << 20)+1),
                (dict(value, format='q4t-profile-business-v19'), 3 << 20),
                (dict(value, reused_business={}), 3 << 20)):
            with self.assertRaises(ValueError):
                profile_record.shape(bad, size)

    def test_false_true_review_values_are_not_equal_to_boolean_contract(self):
        value = r.attach(self.value)
        value['reused_business']['review']['model_quality_repaired'] = 0
        with self.assertRaises(ValueError):
            r.validate(value)


if __name__ == '__main__':
    unittest.main()
