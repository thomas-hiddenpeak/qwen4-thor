"""Fixed reviewed source digests without recursively copying old certificates.

Every current business validator still runs. Only the exact accepted v18
record's untouched fields may be carried forward; new audio is actual work.
"""
import copy

from tools.serve_recovery import ledger
from tools.serve_recovery.profile_speech_source import REVIEW, SOURCES
from tools.serve_recovery.profile_speech_source import (
    CANCEL_REPAIR, CANCEL_REVIEW, CANCEL_SOURCES)

FORMAT = 'q4t-speech-source-reuse-v1'
CANCEL_FORMAT = 'q4t-speech-cancellation-source-reuse-v1'


def old_capabilities(value):
    result = copy.deepcopy(value)
    result['protocol_source_sha256'] = REVIEW['previous_protocol']
    chat = result['realtime']['conversation']
    del chat['speech_output']
    chat['output_modalities'] = ['text']
    chat['pending_execution'] = ['audio', 'mcp', 'tracing']
    return result


def validate(value):
    if value.get('reused_business', {}).get('format') == CANCEL_FORMAT:
        return validate_cancellation(value)
    from tools.serve_recovery import profile_evidence as evidence
    reuse = value['reused_business']; source = SOURCES[value['profile']]
    ledger.keys(reuse, ('format', 'review', 'source', 'keys', 'partial_keys'))
    ledger.require(reuse['format'] == FORMAT and value['format'] == evidence.SPEECH_FORMAT and
        ledger.canonical(reuse['review']) == ledger.canonical(REVIEW) and
        ledger.canonical(reuse['source']) == ledger.canonical(source),
        'unknown accepted speech source review')
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity, binary_sha256=REVIEW['previous_binary'],
            protocol_sha256=REVIEW['previous_protocol'])) == source['identity'] and
        ledger.digest(value['capacity']) == source['capacity'], 'engine/backend/capacity drift')
    ledger.require(evidence.speech_enabled(value['capabilities']) and
        ledger.digest(old_capabilities(value['capabilities'])) == source['capabilities'],
        'source reuse cannot conceal another capability change')
    required = set(source['business'])
    ledger.require(reuse['keys'] == sorted(required - {'tts'}) and reuse['partial_keys'] == ['tts'] and
        set(value['business']) == required | {'realtime_speech'},
        'changed scope cannot reuse old evidence')
    for key in required - {'tts'}:
        ledger.require(ledger.digest(value['business'][key]) == source['business'][key],
                       'retained business drift: ' + key)
    tts = value['business']['tts']
    ledger.require(set(tts) == set(source['tts']) | {'chat_response', 'health'},
                   'shared Chat audio work missing')
    for key, digest in source['tts'].items():
        ledger.require(ledger.digest(tts[key]) == digest, 'standalone TTS changed: ' + key)
    return reuse


def attach(value):
    result = copy.deepcopy(value); source = SOURCES[value['profile']]
    result['reused_business'] = dict(format=FORMAT, review=copy.deepcopy(REVIEW),
        source=copy.deepcopy(source), keys=sorted(set(source['business']) - {'tts'}),
        partial_keys=['tts'])
    validate(result)
    return result


def validate_cancellation(value):
    """Only the reviewed accounting repair may retain the exact old v19 work.

    Retained profile observations remain historical; the separate repair
    witness covers actual interrupted work and same-model continuation.
    Ordinary business validators still run before this source comparison.
    """
    from tools.serve_recovery import profile_evidence as evidence
    source = CANCEL_SOURCES[value['profile']]
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'review', 'source', 'repair', 'keys',
                        'prior_reuse_sha256'))
    ledger.require(value['format'] == evidence.SPEECH_FORMAT and
        reuse['format'] == CANCEL_FORMAT and
        ledger.canonical(reuse['review']) == ledger.canonical(CANCEL_REVIEW) and
        ledger.canonical(reuse['source']) == ledger.canonical(source) and
        ledger.canonical(reuse['repair']) == ledger.canonical(CANCEL_REPAIR) and
        reuse['prior_reuse_sha256'] == source['prior_reuse'],
        'unknown cancellation review, repair or retained source')
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == CANCEL_REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == CANCEL_REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity,
            binary_sha256=CANCEL_REVIEW['previous_binary'],
            protocol_sha256=CANCEL_REVIEW['previous_protocol'])) == source['identity'],
        'cancellation reuse cannot change engine or backends')
    for field, key in (('capabilities', 'protocol_source_sha256'),
                       ('configuration', 'protocol_sha256')):
        observed = value[field]
        ledger.require(observed[key] == CANCEL_REVIEW['candidate_protocol'] and
            ledger.digest(dict(observed, **{key: CANCEL_REVIEW['previous_protocol']})) ==
            source[field], 'cancellation reuse changed ' + field)
    for field in ('capacity', 'startup_memory', 'environment'):
        ledger.require(ledger.digest(value[field]) == source[field],
                       'retained observation changed: ' + field)
    required = set(source['business'])
    ledger.require(reuse['keys'] == sorted(required) and
        set(value['business']) == required, 'cancellation reuse changed business scope')
    for key in required:
        ledger.require(ledger.digest(value['business'][key]) == source['business'][key],
                       'retained cancellation business changed: ' + key)
    return reuse


def attach_cancellation(value):
    """Preserve old profile evidence, then bind the exact repaired identity."""
    result = copy.deepcopy(value)
    source = CANCEL_SOURCES[value['profile']]
    ledger.require(ledger.digest(value['reused_business']) == source['prior_reuse'],
                   'original profile provenance changed')
    for field in ('identity', 'capabilities', 'configuration'):
        ledger.require(ledger.digest(value[field]) == source[field],
                       'original cancellation source changed: ' + field)
    result['identity'].update(binary_sha256=CANCEL_REVIEW['candidate_binary'],
                             protocol_sha256=CANCEL_REVIEW['candidate_protocol'])
    result['capabilities']['protocol_source_sha256'] = CANCEL_REVIEW['candidate_protocol']
    result['configuration']['protocol_sha256'] = CANCEL_REVIEW['candidate_protocol']
    result['reused_business'] = dict(format=CANCEL_FORMAT,
        review=copy.deepcopy(CANCEL_REVIEW), source=copy.deepcopy(source),
        repair=copy.deepcopy(CANCEL_REPAIR), keys=sorted(source['business']),
        prior_reuse_sha256=source['prior_reuse'])
    validate_cancellation(result)
    return result
