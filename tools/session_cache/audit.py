"""Audit current lazy suffix and session lifecycle; no inference or sampling."""
import json
import re
from pathlib import Path

from run_stage import multi, save


def runtime(raw, count):
    fields = lambda line: dict(re.findall(r'(\w+)=(\S+)', line))
    admitted = [fields(l) for l in raw.splitlines() if 'event=admitted ' in l]
    ended = [fields(l) for l in raw.splitlines() if 'event=end ' in l]
    if (len(admitted) != count or len(ended) != count or
            len({r['id'] for r in admitted}) != count or
            {r['id'] for r in admitted} != {r['id'] for r in ended} or
            any(r.get('released') != '1' or r.get('outcome') not in
                ['success', 'cancelled'] for r in ended)):
        raise RuntimeError('request release/outcome')
    suffix = [fields(l) for l in raw.splitlines() if l.startswith('[suffix] ')]
    for row in suffix:
        multi.lazy.audit_stats(row)
    tables = [fields(l) for l in raw.splitlines() if l.startswith('[suffix-classes] ')]
    if suffix:
        if (len(suffix) != count or {r['request'] for r in suffix} !=
                {r['id'] for r in admitted} or len(tables) != 2 or
                [r['event'] for r in tables] != ['configure', 'release'] or
                tables[0].get('lazy') != '1' or tables[0].get('bytes') != '0'):
            raise RuntimeError('lazy classification lifecycle')
        for key, local in [('decodes','cache_decodes'), ('lookups','cache_lookups'),
                           ('allocations','cache_allocations'), ('checks','numeric_checks')]:
            if int(tables[-1][key]) != sum(int(r[local]) for r in suffix):
                raise RuntimeError('classification attribution')
    elif tables:
        raise RuntimeError('classification cache while suffix off')
    batches = [{k:int(v) for k,v in fields(l).items()} for l in raw.splitlines()
               if l.startswith('[mtp-batches] ')]
    hist = [fields(l) for l in raw.splitlines() if l.startswith('[mtp-accept] ')]
    if hist:
        if (len(hist) != count or
                sum(sum(map(int,r['draft_counts'].split(','))) for r in hist) !=
                sum(r['steps'] * r['rows'] for r in batches) or
                sum(r['steps'] * r['suffix'] for r in batches) !=
                sum(int(r['hits']) for r in suffix)):
            raise RuntimeError('verify/extend work ledger')
    streams = [fields(l) for l in raw.splitlines() if l.startswith('[mtp-prefill] ')]
    if streams and (len(streams) != count or any(r['ready'] != '1' or
            int(r['trunk_bytes']) > 4096 * 10240 * 2 for r in streams)):
        raise RuntimeError('bounded prefill trunk')
    sessions = [fields(l) for l in raw.splitlines() if l.startswith('[session-cache] request=')]
    if any(r.get('error') != 'none' for r in sessions):
        raise RuntimeError('session IO/state error')
    return dict(passed=True, requests=count, lifecycle=ended, suffix=suffix,
                tables=tables, batches=batches, sessions=sessions)


def audit_server(out, count):
    # This base checks startup ownership, clean exit and HTTP results. The
    # higher eager-allocation admission audit predates the delivered lazy path.
    multi.lazy.admission._BASE_AUDIT(out, count)
    result = runtime((out / 'server.log').read_text(), count)
    save(out / 'session-audit.json', result)
