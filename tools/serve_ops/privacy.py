"""Allowlisted diagnostic log projection; raw logs remain private operator data."""
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import stat

from .contracts import ABORTS, RECEIVE, STATES, WRITE, InvalidObservation

MAX_LOG = 2*1024**2
MAX_EVENTS = 512
ERRORS = set(ABORTS + RECEIVE + WRITE + STATES) | {
    'handler_error', 'completed', 'capacity_exhausted', 'queue_capacity_exhausted',
    'structured_capacity_exhausted', 'constraint_compile_timeout',
    'constraint_resource_exhausted', 'constraint_worker_failed',
    'structured_output_incomplete', 'structured_output_invalid',
    'response_parse_failed', 'gpu_unhealthy', 'internal_error',
    'invalid_request', 'invalid_cancellation', 'request_not_found',
    'body_capacity_exhausted', 'observation_capacity_exhausted',
    'invalid_request_id_header'}
ENUMS = {
    'event': {'begin', 'registered', 'stage', 'admitted', 'end'},
    'stage': {'validation', 'grammar_compile', 'encode', 'slot_admission',
              'prefill', 'decode', 'response'},
    'outcome': {'success', 'error', 'cancelled'},
    'checkpoint': {'headers', 'prepare', 'execute'},
    'origin': {'handler', 'heartbeat'},
    'reason': ERRORS, 'error': ERRORS,
}
INTEGERS = {'instance', 'seq', 'released', 'status', 'system_error', 'poll_events',
            'transferred', 'scheduler_waits'}
TIMINGS = {'elapsed_ms', 'slot_wait_ms', 'model_lock_ms', 'tokenizer_lock_ms',
           'scheduler_completion_ms', 'stream_write_ms'}
IDS = {'id', 'trace_id', 'completion_id'}
KINDS = {'request', 'http', 'http-receive', 'http-write', 'memory-admission'}


def project(raw, *, salt=None):
    salt = secrets.token_bytes(32) if salt is None else salt
    events, omitted, unknown = [], 0, 0
    for line in raw.splitlines():
        match = re.fullmatch(r'\[q4t\]\[([a-z-]+)\] ([^\r\n]+)', line)
        if not match or match[1] not in KINDS:
            omitted += 1
            continue
        row, seen = dict(kind=match[1]), set()
        bad = False
        for part in match[2].split():
            key, sep, value = part.partition('=')
            if not sep or key in seen:
                bad = True
                break
            seen.add(key)
            if key in IDS and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value):
                row[key+'_hash'] = hmac.new(salt, value.encode(), hashlib.sha256).hexdigest()
            elif key in ENUMS:
                row[key] = value if value in ENUMS[key] else 'unknown'
                unknown += value not in ENUMS[key]
            elif key in INTEGERS and re.fullmatch(r'-?[0-9]{1,16}', value):
                number = int(value)
                if -1 <= number <= 2**53-1:
                    row[key] = number
            elif key in TIMINGS and re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,6})?', value):
                number = float(value)
                if math.isfinite(number):
                    row[key] = number
            else:
                unknown += 1
        if bad or len(row) == 1:
            omitted += 1
        else:
            events.append(row)
    clipped = max(0, len(events)-MAX_EVENTS)
    return dict(events=events[-MAX_EVENTS:], omitted_lines=omitted,
        omitted_older_events=clipped, unknown_fields=unknown,
        id_policy='per_bundle_hmac_sha256; key discarded; no cross_bundle_correlation',
        raw_log_included=False)


def read_log(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise InvalidObservation('log_not_regular')
            start = max(0, before.st_size-MAX_LOG)
            os.lseek(fd, start, os.SEEK_SET)
            raw = bytearray()
            while len(raw) < before.st_size-start:
                chunk = os.read(fd, min(65536, before.st_size-start-len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            named = Path(path).stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or after.st_size < before.st_size:
                raise InvalidObservation('log_rotated_or_truncated')
            if start:
                _, _, raw = raw.partition(b'\n')
            if not raw.endswith(b'\n'):
                raw = raw.rpartition(b'\n')[0]
            projection = project(bytes(raw).decode('utf-8', errors='replace'))
            projection.update(state='available', tail_window_bytes=MAX_LOG,
                              sampled_bytes=len(raw), tail_only=start > 0)
            return projection
        finally:
            os.close(fd)
    except OSError:
        raise InvalidObservation('log_unavailable') from None
