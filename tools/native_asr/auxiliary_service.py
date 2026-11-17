"""Actual optional audio work for managed service qualification, not runtime."""
import base64
import copy
import hashlib

from tools.serve_recovery import profile_evidence as evidence


def selected(identity, stream=False):
    value = copy.deepcopy(identity)
    if stream:
        value['native_asr'].pop('alignment', None)
    return value


def request(identity, stream=False):
    value = selected(identity, stream)
    if evidence.audio_auxiliaries(value):
        result = evidence.auxiliary_audio_request(value)
    else:
        result = dict(model=value['native_asr']['model'], response_format='json',
                      include=['logprobs'])
    if stream:
        result['stream'] = True
    return result


def source(original, identity):
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
