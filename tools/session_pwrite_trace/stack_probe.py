"""One CPU-only real-helper collision test followed by abnormal finalization."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from owned_collector import Collector
from lifecycle import records, signed


def main():
    program, out = map(Path, sys.argv[1:3])
    deadline = float(sys.argv[3])
    out.mkdir()
    with (out/'collector.log').open('x') as log:
        collector = Collector(program, out, deadline, log)
        try:
            (out/'target.json').write_text(json.dumps(dict(pid=os.getpid())))
            while not (out/'target-ready.json').exists():
                if time.time() >= deadline or collector.poll() is not None:
                    raise RuntimeError('bind failed')
                time.sleep(.01)
            waiter = threading.Event()
            for _ in range(16):
                time.sleep(.002)
                waiter.wait(.002)
            (out/'inject-failure').touch()
            if collector.wait(timeout=10) != 2:
                raise RuntimeError('expected failed collector')
        finally:
            if collector.poll() is None:
                collector.terminate()
                collector.wait(timeout=10)
    audit(out)


def audit(out):
    row = json.loads((out/'exit.json').read_text())
    events = list(records(out/'events.bin'))
    ids = [signed(e['d'][0]) for e in events if e['kind'] == 50]
    stacks = json.loads((out/'stacks.json').read_text())
    assert row['status'] == 'FAILED' and 'injected userspace' in row['error']
    assert row['events'] == row['persisted_events'] == row['counters'][1] == len(events)
    assert not row['pending_bytes'] and not row['partial_record_bytes']
    assert not row['finalization_errors']
    assert row['stack_diagnostic']['first_errno'] == 17
    assert row['stack_diagnostic']['recovered'] >= 10
    assert ids and all(i >= 0 and stacks.get(str(i)) for i in ids)
    assert len({i for i in ids if i >= 4096}) >= 10
    result = dict(passed=True, events=len(events), stack_events=len(ids),
                  stack_ids=len(stacks), forced_failure=True,
                  real_helper_errno=17, storage_workload_bytes=0,
                  finished=time.time())
    (out/'check.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    if sys.argv[1] == '--audit-only':
        audit(Path(sys.argv[2]))
    else:
        main()
