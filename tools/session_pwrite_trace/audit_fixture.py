"""Audit all captured records; missing secondary completions fail the gate."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

out = Path(sys.argv[1])
raw = (out/'events.bin').read_bytes()
assert len(raw) % 128 == 0
events = [dict(ts=r[0], task=r[1], key=r[2], type=r[3], cpu=r[4], data=r[5:])
          for r in struct.iter_unpack('<QQQII12Q', raw)]
fixture = json.loads((out/'fixture.json').read_text())
exit_row = json.loads((out/'exit.json').read_text())
counts = Counter(e['type'] for e in events)
issues = [e for e in events if e['type'] == 11]
complete = [e for e in events if e['type'] == 12]
issued_bytes = sum(e['data'][3] for e in issues if e['data'][5] & 255 == 1)
completed_bytes = sum(e['data'][7] for e in complete if e['data'][5] & 255 == 1)
issue_keys = {(e['key'],e['data'][0]) for e in issues}
complete_keys = {(e['key'],e['data'][0]) for e in complete}
checks = dict(
    syscall_count=counts[1] == counts[2] == len(fixture['calls']) == 8,
    exact_bytes=issued_bytes == completed_bytes == fixture['writes'],
    block_lifetimes=issue_keys == complete_keys and len(issues) == len(complete),
    no_ring_loss=exit_row['counters'][2:] == [0,0,0],
    events_flushed=counts.total() == exit_row['events'] == exit_row['counters'][1],
    kernel_stacks=exit_row['stack_ids'] > 0,
    nvme_setup_records=counts[20] == counts[30],
    nvme_complete_records=counts[21] == counts[31],
    nvme_cq_records=counts[22] == counts[32])
result = dict(passed=all(checks.values()), checks=checks, event_counts=dict(counts),
    fixture_write_bytes=fixture['writes'], block_write_bytes=issued_bytes,
    block_complete_write_bytes=completed_bytes,
    missing_nvme_complete=counts[21]-counts[31],
    missing_nvme_cq=counts[22]-counts[32],
    recorded_at=time.time(),
    events_sha256=hashlib.sha256(raw).hexdigest(),
    decision='TOOL_UNAVAILABLE_INCOMPLETE_CORRELATION',
    model_loads=0,http_requests=0,
    limits=['No claim of correct FD/CID/request reuse parser contracts.',
            'No model slow-write reproduction or three-layer attribution.',
            'Raw request probes retain tracked requests after active syscall ends;',
            'formatted NVMe probes only record while an owned syscall is active.',
            'Zero ring loss does not mean the selection filter captured every event.',
            'Original records are preserved, including the useful byte-closed portion.'])
(out/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['passed','checks','missing_nvme_complete',
                                     'missing_nvme_cq','recorded_at']}))
