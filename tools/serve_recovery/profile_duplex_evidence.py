"""Native same-conversation audio, bounded ownership and exact raw receipts."""
import copy
import re
from tools.serve_recovery import ledger
from tools.serve_realtime import duplex_checks

FORMAT = 'q4t-profile-realtime-duplex-v1'
DECLARATION = dict(implemented=True, profile='q4t-realtime-input-v1',
    backend='native_asr', same_conversation=True, default_turn='manual',
    public_transcription='opt_in', wait_for_owned_asr=True,
    vad_create_response=True, vad_interrupt_response=True,
    idle_timeout_ms_min=5000, idle_timeout_ms_max=30000,
    idle_clock='response_done_plus_audio_playback',
    pipeline_recovery=dict(cooldown_ms=5000, attempts_per_fixed_minute=2,
                           model_restart=False),
    delete=True, retrieve=True, automatic_replay=False,
    direct_audio_items=True, mixed_content=True, response_input_audio=True,
    transcript_reference_is_model_input=False,
    admission='serialized_native_workers_per_session',
    additional_host_reservation_bytes=1536 << 20)
PHRASE = '甚至出现交易几乎停滞的情况。'


def declaration(configured):
    ledger.require(type(configured) is bool, 'native input configuration missing')
    return dict(copy.deepcopy(DECLARATION), configured=configured)


def state(value):
    ledger.keys(value, ('admitted','settled','reserved','active',
                        'sessions','host_bytes','free_slots'))
    ledger.require(all(ledger.integer(v) for v in value.values()) and
        all(value[k] == 0 for k in ('reserved','active','sessions','host_bytes')) and
        value['free_slots'] == 1, 'duplex owned resources not drained')


def business(value):
    ledger.keys(value, ('format','source','sessions'))
    ledger.require(value['format'] == FORMAT, 'duplex evidence format')
    source = value['source']
    ledger.keys(source, ('path','sha256','version'))
    ledger.require(source['path'] == 'docs/STATUS.md' and
        type(source['sha256']) is str and ledger.HASH.fullmatch(source['sha256']) and
        type(source['version']) is str and re.fullmatch(r'rev[0-9]+', source['version']),
        'duplex actual project record identity')
    ledger.keys(value['sessions'], ('manual','vad'))
    for mode in ('manual','vad'):
        witness = value['sessions'][mode]
        ledger.keys(witness, ('session','before','after'))
        session = witness['session']
        ledger.require(len(ledger.canonical(session)) <= 4 << 20 and
                       len(session['events']) <= 8192, 'duplex receipt bound')
        observed = duplex_checks.observe(session, PHRASE, source)
        ledger.require(all(q['status']=='PASS' for q in observed['quality']),
                       'duplex original model quality failed')
        before, after = witness['before'], witness['after']
        state(before);state(after)
        settled = after['settled']-before['settled']
        ledger.require(after['admitted']-before['admitted']==observed['admitted'] and
            (settled > observed['visible_tokens'] if observed['private_asr'] else
             settled == observed['visible_tokens']),
            'duplex actual input/response admission or settlement differs')
        # Private ASR has no public token event. Its positive counter residual
        # is retained as a service observation, never an independent oracle.
    return value
