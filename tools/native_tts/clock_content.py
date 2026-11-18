"""Owner-approved 4x content check; listening remains a separate open item.

This is qualification tooling, never a production audio transform. The ASR
receives only measured features with their time axis expanded, never the text.
Original direct-ASR failures remain immutable. Only the final independent
comparison has access to the expected sentence.
"""
from array import array
import math

try:
    from .utterance_acceptance import normalize
except ImportError:
    from utterance_acceptance import normalize


def damage_pcm(pcm, kind):
    """Keep duration unchanged; remove all content or the final three quarters."""
    if not isinstance(pcm, array) or pcm.typecode != 'f' or not 640 <= len(pcm) <= 120000:
        raise ValueError('bounded float32 PCM required')
    if not all(math.isfinite(v) and abs(v) <= 1 for v in pcm):
        raise ValueError('invalid PCM')
    if kind not in ('silence', 'first_quarter'):
        raise ValueError('unfrozen negative control')
    output = array('f', [0]) * len(pcm)
    retained = len(pcm) // 4 if kind == 'first_quarter' else 0
    output[:retained] = pcm[:retained]
    return output


def check_content(text, receipt, case, *, negative=False):
    """Do not count execution/format failure as successful negative detection."""
    tokens = receipt.get('generated_tokens')
    if receipt.get('eos') is not True or type(tokens) is not int or not 0 < tokens <= 64:
        raise ValueError('incomplete or invalid ASR execution')
    expected = normalize(case['text'])
    if not expected:
        raise ValueError('empty expected content')
    language = None
    transcript = text
    if text:
        if text.count('<asr_text>') != 1:
            raise ValueError('missing or duplicate ASR marker')
        prefix, transcript = text.split('<asr_text>')
        if not prefix.startswith('language ') or not prefix[9:].isalpha():
            raise ValueError('invalid ASR language prefix')
        language = prefix[9:].casefold()
        if '<' in transcript or '>' in transcript:
            raise ValueError('unexpected transcript control token')
    exact = normalize(transcript) == expected
    # A wrong language label cannot conceal hallucination of the full sentence.
    passed = not exact if negative else exact and language == case['language']
    return dict(expected=case['text'], raw_transcript=text,
                transcript=transcript, language=language,
                exact_normalized_content=exact, negative_control=negative,
                passed=bool(passed), eos=True, generated_tokens=tokens,
                listening_qualified=False)
