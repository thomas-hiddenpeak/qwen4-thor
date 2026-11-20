"""Compose fixed actual tasks without changing any failed original observation."""
from tools.serve_recovery import ledger, profile_duplex_evidence as d
from tools.serve_recovery.profile_duplex_composed_source import OBSERVED, REVIEW
from tools.serve_realtime import duplex_checks as c, duplex_task_checks as tasks

FORMAT = 'q4t-profile-realtime-duplex-composed-v1'


def settled(witness, observed):
    before, after = witness['before'], witness['after']
    d.state(before)
    d.state(after)
    tokens = after['settled'] - before['settled']
    ledger.require(after['admitted'] - before['admitted'] == observed['admitted']
        and (tokens > observed['visible_tokens'] if observed['private_asr'] else
             tokens == observed['visible_tokens']),
        'composed task admission or settlement differs')


def original_vad(value):
    """Only the first two completed turns qualify; the failed interrupt stays."""
    ledger.require(value['status'] == 'FAIL' and
        value['error'] == 'response status: completed' and
        value['connections'] == 1 and value['automatic_replay'] is False and
        value['response_creates'] == 1 and value['close_code'] is None,
        'original VAD failure changed')
    events = value['events']
    ledger.require(len(events) <= 8192 and
        len({e['event_id'] for e in events}) == len(events) and
        not any(e['type'] == 'error' for e in events), 'original VAD events')
    done = [e for e in events if e['type'] == 'response.done']
    starts = [e for e in events
              if e['type'] == 'input_audio_buffer.speech_started']
    ledger.require(len(done) == 3 and len(starts) == 2 and
        done[-1]['response']['status'] == 'completed' and
        events.index(done[-1]) < events.index(starts[-1]),
        'original response must have ended before attempted interruption')
    # This local view has no successful session status, close or cancellation.
    # It checks the unchanged prefix of the original journal, not a new run.
    prefix = dict(value, mode='idle',
                  events=events[:events.index(done[1]) + 1])
    responses, _ = c.response_rows(prefix)
    commits = [e for e in prefix['events']
               if e['type'] == 'input_audio_buffer.committed']
    idle = [e for e in prefix['events']
            if e['type'] == 'input_audio_buffer.timeout_triggered']
    ledger.require(len(commits) == 2 and len(idle) == 1 and
        commits[0]['item_id'] == starts[0]['item_id'] and
        commits[1]['item_id'] == idle[0]['item_id'] and
        idle[0]['audio_start_ms'] == idle[0]['audio_end_ms'],
        'actual VAD/zero-sample idle ownership differs')
    ledger.require(not any(e['type'].startswith(
        'conversation.item.input_audio_transcription.') and
        e['item_id'] == idle[0]['item_id'] for e in events),
        'zero-input ASR success fabricated')
    _, asr = c.input_rows(prefix['events'], [(commits[0]['item_id'], 0)],
                          d.PHRASE)
    quality = [c.judgment('vad_auto', c.text(responses['vad_auto']), d.PHRASE),
               c.judgment('idle', c.text(responses['idle']), 'READY')]
    ledger.require(value['quality'] == quality and
        all(q['status'] == 'PASS' for q in quality + asr),
        'completed VAD/idle task quality failed')
    # The interrupted SDK did not record final metrics. No full-session
    # settlement, graceful close or cancellation is claimed for this receipt.


def light(data):
    ledger.keys(data, ('original_source', 'original_manual', 'original_vad',
                       'tasks', 'cancel'))
    witness = data['original_manual']
    ledger.keys(witness, ('session', 'before', 'after'))
    observed = c.observe(witness['session'], d.PHRASE, data['original_source'])
    failures = {q['label']: q['actual'] for q in observed['quality']
                if q['status'] == 'FAIL'}
    ledger.require(failures == {
        'inline_audio': '\nRepeat this sentence.\n',
        'roundtrip': 'Ready',
        'tool_answer': '\n\n甚至出现交易几乎停滞的情况。\n\n',
        'asr_temporary_roundtrip_0': 'Repeat this sentence.'},
        'original model failures changed')
    settled(witness, observed)
    original_vad(data['original_vad'])
    for key, mode in (('tasks', 'speech_tool'), ('cancel', 'barge_in')):
        witness = data[key]
        ledger.keys(witness, ('source', 'session', 'before', 'after', 'journal'))
        ledger.require(witness['session']['mode'] == mode, 'wrong task owner')
        observed = tasks.observe(witness['session'], d.PHRASE,
                                 witness['source'], witness['journal'])
        ledger.require(all(q['status'] == 'PASS' for q in observed['quality']),
                       'actual clarified task or audio cancellation failed')
        settled(witness, observed)


def business(value):
    ledger.keys(value, ('format', 'profile', 'source_review', 'data'))
    name = value['profile']
    ledger.require(value['format'] == FORMAT and name in OBSERVED and
        ledger.canonical(value['source_review']) == ledger.canonical(REVIEW) and
        ledger.digest(value['data']) == OBSERVED[name]['duplex_digest'],
        'unknown composed source or modified original observation')
    if name == 'primary':
        d.original_business(value['data'])
    else:
        light(value['data'])
    return value
