"""Acceptance-only projections shared outside pinned production namespaces.

Only the shipped evidence/package readers are imported from recovery namespaces;
no HTTP client, model launcher or unpackaged recovery driver is required here.
"""
import base64
import copy
import hashlib
import zlib

from tools.serve_recovery import profile_evidence as evidence
from tools.serve_service.package import asr_auxiliary_observed_limits, require


def compact(result, continuation):
    # Preserve the complete raw SDK stream beside this lossless projection.
    value = copy.deepcopy(result)
    value['events'] = [e for e in value['events']
                       if e['type'] != 'conversation.item.retrieved']
    value.pop('feed', None)
    evidence.semantic_turn(value, continuation)
    content = value['retrieved']['item']['content'][0]
    raw = evidence.semantic_pcm(content['audio'])
    content['audio'] = dict(encoding='zlib-base64', pcm_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        data=base64.b64encode(zlib.compress(raw, 9)).decode())
    require(evidence.semantic_pcm(content['audio']) == raw,
            'semantic receipt compression changed original PCM')
    return value


def configuration(config):
    vad = asr_auxiliary_observed_limits(config, 'vad')
    require(type(vad['max_chunks']) is int and 0 < vad['max_chunks'] < 2**64,
            'native VAD chunk limit missing or invalid')
    return dict(asr_health=config['native_asr_health'],
        vad_health=config['native_vad_health'], asr_limits=config['native_asr_limits'],
        vad_limits=vad)
