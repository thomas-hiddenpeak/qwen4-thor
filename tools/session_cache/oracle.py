"""Bind a quality answer to the actual prompt, never its token-count tie."""
import hashlib


def short_answer(plan):
    digest = hashlib.sha256(plan['short_prompt'].encode()).hexdigest()
    record = plan['quality'][digest]
    if record['prompt_sha256'] != digest:
        raise ValueError('quality oracle identity mismatch')
    return record['expected']
