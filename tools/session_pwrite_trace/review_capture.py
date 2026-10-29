"""Offline review of the frozen fixture; never starts tracing or I/O loads.

This is a fixture-specific salvage review, not the unimplemented general
FD/merge/requeue/partial-completion parser. The original audit is immutable.
"""
import bisect
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import struct
import sys


def review(out):
    raw = (out / 'events.bin').read_bytes()
    rows = list(struct.iter_unpack('<QQQII12Q', raw))
    pending = {}
    descriptions = {}
    pairs = []
    for index, row in enumerate(rows):
        context = (row[1], row[4])
        if row[3] == 20:
            assert context not in pending
            pending[context] = (index, row)
        elif row[3] == 30:
            before, setup = pending.pop(context)
            payload = struct.pack('<12Q', *row[5:])
            ctrl, queue = struct.unpack_from('<ii', payload, 32)
            cid = struct.unpack_from('<H', payload, 44)[0]
            assert cid == (setup[12] >> 16) & 65535
            key = (setup[2], setup[5])
            assert key not in descriptions
            descriptions[key] = (ctrl, queue, cid)
            pairs.append(dict(event_gap=index-before,
                              timestamp_delta_ns=row[0]-setup[0]))
    assert not pending
    starts = {r[2]: r[0] for r in rows if r[3] == 1}
    ends = {r[2]: r[0] for r in rows if r[3] == 2}
    raw_complete = [r for r in rows if r[3] == 21]
    normal_by_id = defaultdict(list)
    for row in rows:
        if row[3] == 31:
            payload = struct.pack('<12Q', *row[5:])
            normal_by_id[struct.unpack_from('<iii', payload, 32)].append(row)
    missing = []
    matched = 0
    for row in raw_complete:
        identity = descriptions[(row[2], row[5])]
        # Restrict to the same captured syscall; this fixture has serial calls.
        call = row[7]
        matches = [x for x in normal_by_id[identity]
                   if starts[call] <= x[0] <= ends[call]]
        assert len(matches) <= 1
        if matches:
            matched += 1
        else:
            missing.append(dict(ctrl=identity[0], queue=identity[1],
                                cid=identity[2], call=call, cpu=row[4],
                                timestamp_ns=row[0]))
    symbols = []
    for line in (out / 'kallsyms.txt').read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3:
            symbols.append((int(fields[0], 16), fields[2]))
    symbols.sort()
    addresses = [x[0] for x in symbols]
    stacks = {}
    for key, stack in json.loads((out / 'stacks.json').read_text()).items():
        stacks[key] = [symbols[bisect.bisect_right(addresses, address)-1][1]
                       for address in stack]
    lifetimes = defaultdict(list)
    for row in rows:
        if row[3] in (11, 12):
            lifetimes[(row[2], row[5])].append(row)
    assert all(Counter(r[3] for r in values) == {11: 1, 12: 1}
               for values in lifetimes.values())
    ordered = all(next(r[0] for r in values if r[3] == 11)
                  <= next(r[0] for r in values if r[3] == 12)
                  for values in lifetimes.values())
    return dict(
        events_sha256=hashlib.sha256(raw).hexdigest(),
        scope='OFFLINE_FIXTURE_ONLY_NOT_GENERAL_PARSER_ACCEPTANCE',
        setup_pairs=len(pairs), exact_cid_pairs=len(pairs),
        setup_event_gaps=dict(Counter(p['event_gap'] for p in pairs)),
        setup_max_timestamp_gap_ns=max(p['timestamp_delta_ns'] for p in pairs),
        raw_complete_with_setup_identity=len(raw_complete),
        normal_completions_matched=matched, missing_secondary_complete=missing,
        nvme_raw_events_outside_syscall=sum(
            not any(starts[c] <= r[0] <= ends[c] for c in starts)
            for r in rows if r[3] in (20, 21, 22)),
        captured_lifetimes=len(lifetimes), issue_before_complete=ordered,
        inserted_requests=sum(r[3] == 10 for r in rows),
        read_requests=sum(r[3] == 11 and r[10] & 255 == 0 for r in rows),
        timestamp_inversions_in_ring_order=sum(
            b[0] < a[0] for a, b in zip(rows, rows[1:])),
        symbolized_stacks=stacks,
        limits=[
            'All 265 raw completions survive; six secondary gaps are not '
            'evidence of lost device completions.',
            'Their precise cause remains unproven; pwrite-exit filtering '
            'alone does not explain the observed timestamps.',
            'FD and inode were reused. File generations come from fixture '
            'records, not a validated generic lifecycle parser.',
            'Merge, requeue, partial completion and adversarial loss/reuse '
            'parser contracts were not completed in the tool budget.',
            'No model run, performance claim or promotion of the original '
            'failed capability decision follows from this salvage review.'])


if __name__ == '__main__':
    print(json.dumps(review(Path(sys.argv[1])), indent=2))
