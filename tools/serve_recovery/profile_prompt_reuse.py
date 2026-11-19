"""Audited, exact-build reuse of unchanged backends for the Prompt extension.

This is deliberately not a general 'skip qualification' switch. A different
engine, registry, backend, capacity or source review needs a new audit. All
changed Chat/Realtime/Prompt work is fresh, and the old record remains visible.
"""
import copy
import hashlib

from tools.serve_recovery import ledger

FORMAT = 'q4t-prompt-profile-source-reuse-v1'
REVIEW = dict(
    previous_binary='f5af36c6c5d7232f9b180be8c9100616d0cfbe73b30f77135cb4b91a12eb609d',
    candidate_binary='0d1e2ef1f765a725c91225c8f2a8b435d4809508bf1040da9001f6bf6bfedf20',
    candidate_protocol='ea64a350efe08659068ea7d8dfc80c8a7eda4d4530273461dd4256b3a4d0d281',
    previous_registry='c4d9a4579e8157e0a2c790e939c0688c63576831f5b4562cb9d7ceeb782fca98',
    source_review='30c5634ea0fc86ccfceb18f1e346a6144a9eca8d51f3b21d9acfabbbd4995bb2',
    runtime_diff='cf6a4c4aaac6f535b2647c03df6914e6a70347629161fd017a26c6b757f51419')
FRESH = frozenset(('schema', 'tool_call', 'tool_result', 'tool_reply', 'tool_source',
                   'conversation', 'prompt'))
REUSABLE = frozenset(('embedding', 'media', 'voice', 'speaker', 'separator', 'asr',
                      'asr_auxiliary', 'chat_audio', 'tts', 'realtime', 'semantic_vad'))


def legacy_definition():
    # Omitted api intentionally exercises the existing Responses default.
    return dict(variables=dict(left='string', right='string'), request=dict(
        model='qwen3.8-flash-next', temperature=0, reasoning=dict(effort='none'),
        max_output_tokens=32, input=[dict(role='user',
            content='Compute {{left}} + {{right}}. Reply only with its integer value.')]))


def legacy_request(identifier):
    return dict(prompt=dict(id=identifier, variables=dict(left='13', right='29')),
                input=[], store=False)


def legacy_business(value):
    ledger.keys(value, ('definition', 'registered', 'request', 'response'))
    ledger.require(value['definition'] == legacy_definition(), 'legacy Prompt definition changed')
    registered = value['registered']
    identifier = registered.get('id')
    ledger.require(type(identifier) is str and identifier.startswith('prompt_') and
        registered.get('object') == 'prompt' and registered.get('revision') == 1 and
        registered.get('default_version') == '1' and
        value['request'] == legacy_request(identifier), 'legacy Prompt actual registration differs')
    response = value['response']
    ledger.require(response.get('object') == 'response' and response.get('status') == 'completed' and
        response.get('error') is None and response.get('incomplete_details') is None and
        response.get('prompt', {}).get('id') == identifier and
        response['prompt'].get('version') == '1', 'legacy Prompt actual response identity differs')
    text = ''.join(p['text'] for i in response['output'] if i['type'] == 'message'
                   for p in i['content'] if p['type'] == 'output_text')
    ledger.require(text.strip() == '42' and not any(i['type'] == 'function_call'
        for i in response['output']), 'legacy Prompt actual arithmetic failed')
    usage = response['usage']
    ledger.require(all(ledger.integer(usage[k]) and usage[k] > 0 for k in
        ('input_tokens', 'output_tokens', 'total_tokens')) and
        usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
        'legacy Prompt actual usage missing')


def validate(value):
    """Validate provenance, never mutate or relabel the previous observations."""
    from tools.serve_recovery import profile_evidence as evidence
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'review', 'registry', 'record', 'keys', 'legacy_responses_prompt'))
    ledger.require(value['format'] == evidence.PROMPT_FORMAT and reuse['format'] == FORMAT and
        ledger.canonical(reuse['review']) == ledger.canonical(REVIEW) and
        len(ledger.canonical(reuse)) <= 8 << 20, 'unknown or oversized source reuse contract')
    previous, registry = reuse['record'], reuse['registry']
    ledger.require(previous.get('format') == evidence.CONVERSATION_FORMAT and
        'reused_business' not in previous and ledger.digest(registry) == REVIEW['previous_registry'],
        'only exact original v17 qualification can be reused, never a chain')
    # The original semantic profile writer uses canonical JSON plus one newline.
    digest = hashlib.sha256(ledger.canonical(previous) + b'\n').hexdigest()
    evidence.validate(previous, registry, value['profile'], digest)
    old, new = previous['identity'], value['identity']
    ledger.require(old['binary_sha256'] == REVIEW['previous_binary'] and
        new['binary_sha256'] == REVIEW['candidate_binary'] and
        new['protocol_sha256'] == REVIEW['candidate_protocol'] and
        {k: v for k, v in old.items() if k not in ('binary_sha256', 'protocol_sha256')} ==
        {k: v for k, v in new.items() if k not in ('binary_sha256', 'protocol_sha256')} and
        previous['capacity'] == value['capacity'], 'source reuse engine/model/backend/capacity drift')
    ledger.require(evidence.conversation_enabled(previous['capabilities']) and
        not evidence.prompt_enabled(previous['capabilities']) and evidence.prompt_enabled(value['capabilities']),
        'source reuse must be the reviewed Prompt capability extension')
    current = copy.deepcopy(value['capabilities'])
    current['protocol_source_sha256'] = previous['capabilities']['protocol_source_sha256']
    current['realtime']['conversation'] = copy.deepcopy(previous['capabilities']['realtime']['conversation'])
    ledger.require(ledger.canonical(current) == ledger.canonical(previous['capabilities']),
                   'source reuse cannot hide other capability changes')
    keys = sorted(set(previous['business']) - FRESH)
    ledger.require(type(reuse['keys']) is list and reuse['keys'] == keys and
        set(keys) <= REUSABLE and FRESH <= value['business'].keys() and
        set(value['business']) == FRESH | set(keys), 'fresh/reused business boundary differs')
    for key in keys:
        ledger.require(ledger.canonical(value['business'][key]) ==
            ledger.canonical(previous['business'][key]), 'reused backend observation changed: ' + key)
    legacy_business(reuse['legacy_responses_prompt'])
    return reuse


def attach(value, registry, previous, legacy):
    """Construct an explicit retained-source record after fresh work completes."""
    result = copy.deepcopy(value)
    result['reused_business'] = dict(format=FORMAT, review=copy.deepcopy(REVIEW),
        registry=copy.deepcopy(registry), record=copy.deepcopy(previous),
        keys=sorted(set(previous['business']) - FRESH), legacy_responses_prompt=copy.deepcopy(legacy))
    validate(result)
    return result
