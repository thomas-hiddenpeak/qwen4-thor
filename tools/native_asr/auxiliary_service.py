"""Actual optional audio work for managed service qualification, not runtime."""
import base64
import copy
import hashlib

from tools.serve_recovery import profile_evidence as evidence


def selected(identity, stream=False):
    value = copy.deepcopy(identity)
    if stream and not evidence.has_speaker(value):
        value['native_asr'].pop('alignment', None)
    return value


def request(identity, stream=False):
    value = selected(identity, stream)
    if evidence.has_separator(value):
        result = evidence.separator_request(value)
    elif evidence.has_speaker(value):
        result = evidence.speaker_request(value)
    elif evidence.audio_auxiliaries(value):
        result = evidence.auxiliary_audio_request(value)
    else:
        result = dict(model=value['native_asr']['model'], response_format='json',
                      include=['logprobs'])
    if stream:
        result['stream'] = True
    return result


def source(original, identity):
    if evidence.has_separator(identity):
        evidence.ledger.require(hashlib.sha256(original).hexdigest() == evidence.SEPARATOR_FILE_SHA,
                                'qualified managed separator source differs')
        return original
    if evidence.has_speaker(identity):
        evidence.ledger.require(hashlib.sha256(original).hexdigest() == evidence.SPEAKER_FILE_SHA,
                                'qualified managed speaker source differs')
        return original
    return evidence.auxiliary_audio_source(original, identity)


def fields(value):
    """Preserve array spelling and the actual nested VAD object in multipart."""
    import json
    result = []
    for key, item in value.items():
        values = item if type(item) is list else [item]
        for row in values:
            text = json.dumps(row) if type(row) in (dict, bool) else str(row)
            result.append((key+'[]' if type(item) is list else key, text))
    return result


def check(response, identity, config, audio, stream=False):
    """Use actual source/tokens/times and independent health, never text alone."""
    value = selected(identity, stream)
    if evidence.has_separator(value):
        return separator_result(response, value, config, hashlib.sha256(audio).hexdigest(), stream)
    if evidence.has_speaker(value):
        return speaker_result(response, value, config, hashlib.sha256(audio).hexdigest(), stream)
    names = evidence.audio_auxiliaries(value)
    witness = dict(source_base64=base64.b64encode(audio).decode(),
        request=request(identity, stream), response=response)
    witness['request'].pop('stream', None)
    if names:
        witness.update(health={k:config['native_'+k+'_health'] for k in names},
            limits={k:(config['native_alignment_limits'] if k == 'alignment' else
                dict(max_chunks=config['native_vad_max_chunks'])) for k in names})
        evidence.audio_auxiliary_business(witness, value)
    else:
        witness.update(health=config['native_asr_health'],
                       limits=config['native_asr_limits'])
        evidence.audio_business(witness, value)
    return dict(actual_audio_sha256=hashlib.sha256(audio).hexdigest(),
        auxiliaries=list(names), stream=stream, text=response['text'],
        words=len(response.get('words', [])), segments=len(response.get('segments', [])))


def speaker_result(response, identity, config, source_sha256, stream=False):
    """Shared pure witness checker; the live caller hashes its exact audio bytes."""
    from tools.serve_service import package
    value = dict(source_sha256=source_sha256, request=evidence.speaker_request(identity),
        response=response, health=config['native_speaker_health'],
        limits=config['native_speaker_limits'])
    evidence.speaker_business(value, identity)
    # A ready speaker alone cannot certify the alignment and VAD dependencies.
    for name in ('alignment', 'vad'):
        fixed = identity['native_asr'][name]
        health = config['native_'+name+'_health']
        evidence.ledger.require(health.get('model') == fixed['model'] and
            health.get('state') == 'ready' and health.get('reason') == 'none' and
            health.get('core_readiness_dependency') is False and
            evidence.ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
            package.same_json(package.asr_auxiliary_observed_limits(config, name), fixed['limits']),
            'qualified speaker dependency not healthy or budget changed')
    return dict(actual_audio_sha256=source_sha256, auxiliaries=['alignment','vad','speaker'],
        stream=stream, text=response['text'], segments=len(response['segments']),
        labels=[row['speaker'] for row in response['segments']])


def separator_result(response, identity, config, source_sha256, stream=False):
    """A successful mixed response must retain every configured dependency."""
    from tools.serve_service import package
    value = dict(source_sha256=source_sha256, request=evidence.separator_request(identity),
        response=response, health=config['native_separator_health'],
        limits=config['native_separator_limits'])
    evidence.separator_business(value, identity)
    for name in ('asr', 'alignment', 'vad', 'speaker'):
        fixed = identity['native_asr'] if name == 'asr' else identity['native_asr'][name]
        health = config['native_'+name+'_health']
        limits = (config['native_asr_limits'] if name == 'asr' else
                  package.asr_auxiliary_observed_limits(config, name))
        evidence.ledger.require(health.get('model') == fixed['model'] and
            health.get('state') == 'ready' and health.get('reason') == 'none' and
            health.get('core_readiness_dependency') is False and
            evidence.ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
            package.same_json(limits, fixed['limits']),
            'qualified separator dependency not healthy or budget changed')
    return dict(actual_audio_sha256=source_sha256,
        auxiliaries=['alignment','vad','speaker','separator'], stream=stream,
        text=response['text'], segments=len(response['segments']),
        labels=[row['speaker'] for row in response['segments']])
