"""Synthetic CPU counterexamples, never actual model or recovery evidence."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_realtime import managed as m
from tools.native_asr import service_acceptance as service
from tools.serve_service.package import save

WAVE = b'\x00\x01' * 100894


def state(**updates):
    return dict(dict.fromkeys(m.METRICS, 0), free_slots=1, **updates)


def wire(value):
    return '\n'.join(key+' '+str(value[name]) for name, key in m.METRICS.items())+'\n'


def session(mode='complete', transport='ws-loopback', identifier='cpu-item-1'):
    settings = m.settings(mode == 'hold_stream')
    cfg = settings['audio']['input']
    cfg['transcription'] = dict(language='', prompt='', languages=[], keywords=[],
                                  **cfg['transcription'])
    events = [dict(type='session.created', session=dict(id='cpu-session')),
              dict(type='session.updated', session=settings)]
    value = dict(phase='result', status='PASS', mode=mode, transport=transport,
        connections=1, close_code=1000 if mode == 'complete' else 1006,
        automatic_replay=False, partial=None, turn=None, sent_events=20,
        event_bytes=1024, transport_errors=[], events=events,
        sent_audio_bytes=201790, total_audio_bytes=201790)
    if mode == 'hold_stream':
        value.update(partial=dict(item_id=identifier, delta='甚至'),
                     sent_audio_bytes=24000, total_audio_bytes=33632)
        events.append(dict(type='conversation.item.input_audio_transcription.delta',
                           item_id=identifier, delta='甚至'))
    else:
        committed = dict(type='input_audio_buffer.committed', item_id=identifier,
                         previous_item_id=None)
        done = dict(type='conversation.item.input_audio_transcription.completed',
            item_id=identifier, transcript=m.evidence.ASR_TEXT,
            usage=dict(type='tokens', input_tokens=70, output_tokens=11, total_tokens=81))
        retrieved = dict(type='conversation.item.retrieved', item=dict(id=identifier,
            object='realtime.item', type='message', role='user', status='completed',
            content=[dict(type='input_audio', transcript=m.evidence.ASR_TEXT,
                          audio=base64.b64encode(WAVE).decode())]))
        events.extend([committed,
            dict(type='conversation.item.input_audio_transcription.delta',
                 item_id=identifier, delta=m.evidence.ASR_TEXT), done, retrieved])
        value['turn'] = dict(item_id=identifier, committed=committed, terminal=done,
                            delta=m.evidence.ASR_TEXT, retrieved=retrieved)
    return value


class Evidence(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(m.evidence, 'REALTIME_WAVEFORM_SHA',
                                      hashlib.sha256(WAVE).hexdigest())
        self.patch.start(); self.addCleanup(self.patch.stop)
        temp = tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR'])
        self.addCleanup(temp.cleanup); self.out = Path(temp.name)

    def fixture(self, scenario='tool_version'):
        self.plan = dict(scenario=scenario, realtime=dict(format='q4t-managed-realtime-v1'),
                         limits=dict(websocket_sessions=1 if scenario == 'tool_retained' else 2))
        self.capabilities = dict(realtime=dict(transcription_configured=True,
            automatic_replay=False, per_turn_client_admission=True))
        self.identity = dict(native_asr={})
        transport = 'openai-realtime-wss' if scenario == 'tls' else 'ws-loopback'
        previous = None
        for i, (label, mode) in enumerate(m.phases(self.plan)):
            prefix = label+'-realtime'; identifier = 'cpu-item-'+str(i)
            value = session(mode, transport, identifier)
            before = state(admitted=20 if i == 0 else 0,
                settled=100 if i == 0 else 0, total=20 if i == 0 else 0,
                success=20 if i == 0 else 0)
            if previous is not None and scenario == 'tls':
                before = previous
            # A restarted model legitimately has fresh process counters.
            after = dict(before, admitted=before['admitted']+1,
                settled=before['settled']+(39 if mode == 'hold_stream' else 81),
                total=before['total']+1, success=before['success']+(mode != 'hold_stream'),
                aborted=before['aborted']+(mode == 'hold_stream'))
            previous = after
            row = dict(mode=mode, before=before,
                       after=None if mode == 'hold_complete' else after)
            if mode != 'complete':
                held = dict(before if mode == 'hold_stream' else after,
                    sessions=1, host_bytes=1073741824,
                    active=1 if mode == 'hold_stream' else 0,
                    reserved=300 if mode == 'hold_stream' else 0)
                row['held'] = held
                save(self.out/(prefix+'-ready.json'), dict(phase='ready', mode=mode,
                    transport=transport, connections=1, item_id=identifier,
                    delta=value['partial']['delta'] if mode == 'hold_stream' else m.evidence.ASR_TEXT,
                    sent_audio_bytes=value['sent_audio_bytes'],
                    total_audio_bytes=value['total_audio_bytes']))
            save(self.out/(prefix+'.sdk.json'), value)
            save(self.out/(prefix+'-receipt.json'), row)
            for suffix in ('before', 'after', 'held'):
                if row.get(suffix) is not None:
                    save(self.out/(prefix+'-'+suffix+'-metrics.json'),
                         [dict(http_status=200, body=wire(row[suffix]))])
        save(self.out/'counts.json', dict(counts=self.plan['limits']))

    def check(self):
        return m.check(self.out, self.plan, self.identity, self.capabilities)

    def mutate(self, name, change):
        path = self.out/name; raw = path.read_bytes(); value = json.loads(raw)
        change(value); save(path, value)
        try:
            with self.assertRaises((RuntimeError, ValueError, KeyError)): self.check()
        finally:
            path.write_bytes(raw)

    def test_all_managed_modes_and_new_process_counter_reset(self):
        for scenario in ('tool_retained', 'tool_version', 'tls'):
            self.fixture(scenario)
            result = self.check()
            self.assertEqual(len(result), self.plan['limits']['websocket_sessions'])
            self.assertTrue(all(not row['automatic_replay'] for row in result.values()))
        self.assertEqual(result['tls-before-realtime']['actual_tokens'], 39)

    def test_missing_duplicate_noninteger_metrics_never_become_zero(self):
        raw = wire(state())
        self.assertEqual(m.state(raw), state())
        for broken in (raw.replace('q4t_realtime_sessions 0\n', ''),
                       raw+'q4t_realtime_sessions 0\n',
                       raw.replace('q4t_realtime_sessions 0', 'q4t_realtime_sessions NaN'),
                       raw.replace('q4t_realtime_sessions 0', 'q4t_realtime_sessions -1'),
                       raw.replace('q4t_realtime_sessions 0', 'q4t_realtime_sessions 18446744073709551616')):
            with self.assertRaises(RuntimeError): m.state(broken)
        with self.assertRaises(RuntimeError): m.clean(state(active=False))

    def test_declared_capability_cannot_silently_skip_actual_work(self):
        self.fixture(); self.plan.pop('realtime')
        with self.assertRaises(RuntimeError): self.check()
        self.assertEqual(m.check(self.out, {}, {}, {}), {})
        self.fixture(); self.capabilities = {}
        with self.assertRaises(RuntimeError): self.check()

    def test_transport_retries_and_withheld_input_reject(self):
        for field, bad in (('transport', 'openai-realtime-wss'), ('connections', 2),
                           ('automatic_replay', True), ('close_code', 1000),
                           ('sent_audio_bytes', 33632), ('turn', {})):
            value = session('hold_stream'); value[field] = bad
            with self.assertRaises(RuntimeError): m.check_session(value, 'hold_stream', 'ws-loopback')
        value = session('hold_stream')
        value['events'].append(dict(type='input_audio_buffer.committed'))
        with self.assertRaises(RuntimeError): m.check_session(value, 'hold_stream', 'ws-loopback')

    def test_effective_hints_and_fabricated_partial_reject(self):
        value = session('hold_stream')
        value['events'][1]['session']['audio']['input']['transcription']['prompt'] = 'answer'
        with self.assertRaises(RuntimeError): m.check_session(value, 'hold_stream', 'ws-loopback')
        value = session('hold_stream'); value['events'][-1]['delta'] = 'fabricated'
        with self.assertRaises(RuntimeError): m.check_session(value, 'hold_stream', 'ws-loopback')

    def test_actual_wire_completion_history_and_waveform_required(self):
        for change in (
            lambda v: v['turn']['committed'].update(previous_item_id='prior'),
            lambda v: v['turn']['terminal']['usage'].update(input_tokens=69),
            lambda v: v['turn']['retrieved']['item']['content'][0].update(audio='AAAA'),
            lambda v: v['events'].pop(),
            lambda v: v['events'].append(copy.deepcopy(v['turn']['terminal'])),
            lambda v: v['events'][3].update(delta='different')):
            value = session(); change(value)
            with self.assertRaises((RuntimeError, ValueError)):
                m.check_session(value, 'complete', 'ws-loopback')

    def test_actual_accounting_rejects_replay_unsettled_and_false_success(self):
        before = state()
        after = state(admitted=1, settled=81, total=1, success=1)
        self.assertEqual(m.accounting(before, after, tokens=81), 81)
        for key, wrong in [('admitted', 2), ('total', 2), ('success', 0),
                           ('aborted', 1), ('errors', 1), ('settled', 80)]:
            with self.assertRaises(RuntimeError):
                m.accounting(before, dict(after, **{key: wrong}), tokens=81)
        with self.assertRaises(RuntimeError): m.accounting(before, after, interrupted=True)

    def test_raw_observations_and_clean_reservations_cannot_be_faked(self):
        self.fixture('tls')
        self.mutate('tls-before-realtime-receipt.json',
                    lambda v: v['after'].update(reserved=1))
        self.mutate('tls-before-realtime-receipt.json',
                    lambda v: v['held'].update(active=0))
        self.mutate('tls-before-realtime-after-metrics.json',
                    lambda v: v[0].update(body=wire(state())))
        self.mutate('tls-before-realtime-ready.json',
                    lambda v: v.update(item_id='foreign'))

    def test_payload_cannot_claim_old_counters_after_restart_or_omit_new_session(self):
        self.fixture()
        self.mutate('primary-realtime-receipt.json', lambda v: v.update(after=state()))
        self.mutate('counts.json', lambda v: v['counts'].update(websocket_sessions=1))
        (self.out/'fallback-realtime.sdk.json').unlink()
        with self.assertRaises(FileNotFoundError): self.check()

    def test_exact_work_budgets_and_no_partial_tls_bypass(self):
        for scenario, count, sessions in [('tool_retained', 15, 1),
                                          ('tool_version', 26, 2), ('tls', 27, 2)]:
            limits = service.limits_for(scenario, True, True, native_voice=True, realtime=True)
            self.assertEqual(limits['chats'], count)
            self.assertEqual(limits['websocket_sessions'], sessions)
            plan = dict(scenario=scenario, chat_audio=True, native_tts={}, native_voice={},
                        realtime={}, limits=limits)
            service.configure_plan_limits(plan)
            with self.assertRaises(RuntimeError):
                service.configure_plan_limits(dict(plan, limits=dict(limits, chats=count-1)))
        with self.assertRaises(RuntimeError): service.limits_for('tls_remaining', realtime=True)
        with self.assertRaises(RuntimeError): service.limits_for('tls', realtime=1)

    def test_failed_connection_cannot_be_replayed_or_skip_order(self):
        actor = m.Actor(SimpleNamespace(plan=dict(scenario='tool_version')))
        with self.assertRaises(RuntimeError): actor.start('fallback')
        actor.current = {}; actor.attempted = ['primary']
        with self.assertRaises(RuntimeError): actor.start('primary')
        actor.close()

    def test_bounded_reader_rejects_oversize_and_early_eof(self):
        for data, count in ((b'{}\n', m.MAX_RECEIPT), (b'', 0)):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, data); os.close(write_fd)
            with os.fdopen(read_fd, 'rb', buffering=0) as source:
                actor = m.Actor(None); actor.total_bytes = count
                actor.process = SimpleNamespace(stdout=source)
                with self.assertRaises(RuntimeError): actor.line(1)


if __name__ == '__main__':
    unittest.main()
