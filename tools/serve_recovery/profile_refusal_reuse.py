"""Exact-source reuse after changing the main model's refusal instruction.

Every main-model consumer is fresh. Original standalone backend observations
remain visible; registered-voice Chat has a separate saved-state witness.
"""
import copy
import hashlib

from tools.serve_recovery import ledger, profile_prompt_reuse as previous

FORMAT = 'q4t-prompt-refusal-source-reuse-v1'
ASSEMBLED_FORMAT = 'q4t-prompt-refusal-source-assembly-v1'
ASSEMBLY_DIGEST = '23d3a56705570977cb3571aabcf01220131d36ec0644c18377259b3215e0ba46'
ASSEMBLY_FILE = '67df6a9624e5512ff850cb471ff36944bc2d6a149f4720e7d7b0c924f6058c39'
REVIEW = dict(previous.REVIEW,
    candidate_binary='648d6d68553a45eb3c5fb703b68dba2e63dd1ed7171f68c1c420b599819723f5',
    candidate_protocol='d4a96aa1a71ed2b8af455e626ff69de0f5db88634bc5bd31e16af768f417595b',
    source_review='09c80db683fc69aa0540122832f3fa8c9cca7f8e479e11124744f01d008dbf88',
    runtime_diff='f50623b8840df461a9ecc417d6558523a7868ee12db7a709c23692ba74a568bd')
FRESH = previous.FRESH | {'media', 'chat_audio', 'semantic_vad'}
REUSABLE = frozenset(('embedding', 'asr', 'asr_auxiliary', 'speaker',
                      'separator', 'realtime', 'voice'))
VOICE_SNAPSHOT = 'd34ff45646382226ef95631d556f3d498a3ae3b2af0cf44af3a8b47772f125f2'
VOICE_OWNER = '67927bf6ad595362fb59ef081c9176b6f691d5047d849bb0555d7696636b6c7d'
VOICE_CONSENT = 'cons_39d90c9bc9f0745c16a4ddecafb9bd70'
VOICE_ID = 'voice_4018e051d242e842dd9d99218c329edf'
CONDITION_FIELDS = ('condition', 'reference', 'transcript', 'source_sha256',
                    'condition_model', 'condition_revision')


def voice_business(witness, original, identity):
    from tools.serve_recovery import profile_evidence as evidence
    ledger.keys(witness, ('snapshot_sha256', 'records', 'request', 'chat',
                          'stored_chat', 'chat_transcript', 'health'))
    ledger.require(witness['snapshot_sha256'] == VOICE_SNAPSHOT,
                   'unknown saved voice snapshot')
    records = witness['records']
    evidence.voice_reference(records, VOICE_CONSENT, VOICE_ID)
    ledger.require(all(r['owner'] == VOICE_OWNER for r in records.values()) and
        all(ledger.canonical(records['voice']['data'][k]) ==
            ledger.canonical(original['conditioning']['voice']['data'][k])
            for k in CONDITION_FIELDS), 'saved voice condition or owner drift')
    expected = evidence.voice_requests(VOICE_CONSENT, VOICE_ID)['chat']
    ledger.require(ledger.canonical(witness['request']) == ledger.canonical(expected),
                   'fresh voice Chat request differs')
    chat, stored = witness['chat'], witness['stored_chat']
    ledger.require(all(ledger.canonical(chat[k]) == ledger.canonical(stored[k])
        for k in ('id', 'choices', 'usage')), 'fresh voice stored Chat differs')
    # Reuse only the old standalone Speech half for this shared validator.
    # The original record itself is never rewritten or called a fresh run.
    evidence.voice_synthesis(dict(original, chat=chat,
        chat_transcript=witness['chat_transcript']), identity['native_tts_base'])
    ledger.require(chat['usage']['q4t_audio_synthesis']['input_tokens'] == 918,
                   'fresh voice Chat lost actual ICL reference input')
    health = witness['health']
    ledger.require(health.get('state') == 'ready' and
        health.get('model') == identity['native_tts_base']['model'] and
        health.get('core_readiness_dependency') is False and
        health.get('reason') == 'none' and
        type(health.get('compute_failures')) is int and health['compute_failures'] == 0 and
        health.get('asset_cleanup_pending') is False,
        'fresh saved-voice backend unhealthy')


def validate(value):
    from tools.serve_recovery import profile_evidence as evidence
    reuse = value['reused_business']
    assembled = reuse.get('format') == ASSEMBLED_FORMAT
    ledger.keys(reuse, ('format', 'review', 'registry', 'record', 'keys',
        'partial_keys', 'legacy_responses_prompt', 'voice_prompt') +
        (('assembly',) if assembled else ()))
    ledger.require(value['format'] == evidence.PROMPT_FORMAT and
        reuse['format'] == (ASSEMBLED_FORMAT if assembled else FORMAT) and
        ledger.canonical(reuse['review']) == ledger.canonical(REVIEW) and
        len(ledger.canonical(reuse)) <= 8 << 20,
        'unknown refusal source review or oversized record')
    old, registry = reuse['record'], reuse['registry']
    ledger.require(old.get('format') == evidence.CONVERSATION_FORMAT and
        'reused_business' not in old and ledger.digest(registry) == REVIEW['previous_registry'],
        'only original v17 qualification may supply unchanged backends')
    digest = hashlib.sha256(ledger.canonical(old) + b'\n').hexdigest()
    evidence.validate(old, registry, value['profile'], digest)
    before, after = old['identity'], value['identity']
    ledger.require(before['binary_sha256'] == REVIEW['previous_binary'] and
        after['binary_sha256'] == REVIEW['candidate_binary'] and
        after['protocol_sha256'] == REVIEW['candidate_protocol'] and
        {k: v for k, v in before.items() if k not in ('binary_sha256', 'protocol_sha256')} ==
        {k: v for k, v in after.items() if k not in ('binary_sha256', 'protocol_sha256')} and
        old['capacity'] == value['capacity'], 'engine/model/backend/capacity drift')
    ledger.require(evidence.conversation_enabled(old['capabilities']) and
        not evidence.prompt_enabled(old['capabilities']) and
        evidence.prompt_enabled(value['capabilities']), 'unreviewed capability extension')
    caps = copy.deepcopy(value['capabilities'])
    caps['protocol_source_sha256'] = old['capabilities']['protocol_source_sha256']
    caps['realtime']['conversation'] = copy.deepcopy(old['capabilities']['realtime']['conversation'])
    ledger.require(ledger.canonical(caps) == ledger.canonical(old['capabilities']),
                   'source reuse cannot hide another capability change')
    required = previous.FRESH | (set(old['business']) & FRESH)
    partial = {'tts'} & old['business'].keys()
    retained = set(old['business']) - required - partial
    ledger.require(retained <= REUSABLE and reuse['keys'] == sorted(retained) and
        reuse['partial_keys'] == sorted(partial) and
        set(value['business']) == required | partial | retained,
        'main-model consumer missing or incorrectly reused')
    for key in retained:
        ledger.require(ledger.canonical(value['business'][key]) ==
            ledger.canonical(old['business'][key]), 'retained observation changed: ' + key)
    if 'tts' in partial:
        fresh, source = value['business']['tts'], old['business']['tts']
        ledger.require(set(fresh) == set(source), 'TTS observation shape changed')
        for key in set(source) - {'chat_response', 'health'}:
            ledger.require(ledger.canonical(fresh[key]) == ledger.canonical(source[key]),
                           'retained standalone TTS observation changed: ' + key)
    if 'voice' in retained:
        voice_business(reuse['voice_prompt'], old['business']['voice'], value['identity'])
    else:
        ledger.require(reuse['voice_prompt'] is None, 'unexpected saved voice evidence')
    previous.legacy_business(reuse['legacy_responses_prompt'])
    if assembled:
        validate_assembly(value)
    return reuse


def validate_assembly(value):
    """Bind exact successful fragments and preserve every failed source stage.

    This source review is deliberately fixed to one completed collection. It
    cannot accept an arbitrary later successful sample, discard the original
    failures, or pretend three independent leases were one continuous run.
    """
    reuse = value['reused_business']; proof = reuse['assembly']
    ledger.require(ledger.digest(proof) == ASSEMBLY_DIGEST and
        proof.get('format') == 'q4t-completed-consumer-assembly-v1' and
        proof['identity'] == value['identity'] and
        proof['profiles'][value['profile']] == value['capacity'],
        'unreviewed consumer source assembly')
    selected = proof['profiles_receipts'][value['profile']]
    ledger.require(set(selected['fresh']) == FRESH | {'tts'} and
        all(ledger.digest(value['business'][k]) == digest
            for k, digest in selected['fresh'].items()) and
        ledger.digest(reuse['voice_prompt']) == selected['voice'] and
        ledger.digest(reuse['legacy_responses_prompt']) == selected['legacy'] and
        ledger.digest(value['capabilities']) == selected['capabilities'] and
        ledger.digest(value['configuration']) == selected['configuration'] and
        ledger.digest(value['startup_memory']) == selected['startup_memory'],
        'actual successful consumer or observed capacity changed')
    # This digest names the retained combined source document, as in the
    # existing partial-primary review. Its individual lease hashes remain in
    # the proof; it is explicitly not the hash of a fictitious single run.
    ledger.require(value['environment'] == dict(status='PASS', env_abort=False,
        all_queries_ok=True, cleanup_complete=True, nvml_queries=323,
        lease_result_sha256=ASSEMBLY_FILE), 'combined source environment differs')


def attach(value, registry, original, legacy, voice, *, assembly=None):
    result = copy.deepcopy(value)
    partial = {'tts'} & original['business'].keys()
    result['reused_business'] = dict(
        format=ASSEMBLED_FORMAT if assembly is not None else FORMAT,
        review=copy.deepcopy(REVIEW),
        registry=copy.deepcopy(registry), record=copy.deepcopy(original),
        keys=sorted(set(original['business']) - FRESH - partial),
        partial_keys=sorted(partial), legacy_responses_prompt=copy.deepcopy(legacy),
        voice_prompt=copy.deepcopy(voice))
    if assembly is not None:
        result['reused_business']['assembly'] = copy.deepcopy(assembly)
    validate(result)
    return result
