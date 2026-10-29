"""Bounded failure-safe trace finalization, independent of BPF loading."""
import json
from pathlib import Path


def flush_pending(stream, pending, sync):
    while pending:
        chunk = bytes(pending[:4*1024**2])
        wrote = stream.write(chunk)
        if not isinstance(wrote, int) or not 0 < wrote <= len(chunk):
            raise OSError('trace write made no progress')
        # Consume accepted bytes before sync: a sync failure must not cause
        # those bytes to be appended twice on the failure path.
        del pending[:wrote]
        sync(stream)


def finalize(out, pending, result, detach, drain, snapshot, sync):
    errors = []
    for name, action in [('detach', detach), ('drain', drain)]:
        try:
            action()
        except BaseException as error:
            errors.append(name+': '+repr(error))
    try:
        with (out/'events.bin').open('ab', buffering=0) as stream:
            flush_pending(stream, pending, sync)
    except BaseException as error:
        errors.append('tail flush: '+repr(error))
    try:
        result.update(snapshot())
    except BaseException as error:
        errors.append('snapshot: '+repr(error))
    size = (out/'events.bin').stat().st_size if (out/'events.bin').exists() else 0
    result.update(persisted_events=size//128, partial_record_bytes=size % 128,
                  pending_bytes=len(pending), finalization_errors=errors)
    if errors or pending or size % 128:
        result['status'] = 'FAILED'
    # Preserve an existing failure even when finalization itself succeeds.
    (out/'finalization.json').write_text(json.dumps(result, indent=2)+'\n')
