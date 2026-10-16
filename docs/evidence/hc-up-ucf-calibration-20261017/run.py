import hashlib
import json
from pathlib import Path
import selectors
import subprocess
root = Path(__file__).resolve().parent
probe = root.parent / 'hc-up-ucf-window-20261017/probe'
children = []
rows = []
def receive(p):
    # Each command has exactly one response; select before blocking readline.
    with selectors.DefaultSelector() as s:
        s.register(p.stdout, selectors.EVENT_READ)
        if not s.select(10):
            raise TimeoutError('missing protocol response')
    line = p.stdout.readline()
    if not line:
        raise RuntimeError('unexpected EOF')
    return line.strip()
def send(p, line):
    p.stdin.write(line + '\n'); p.stdin.flush()
try:
    with (root / 'cuda.stderr').open('w') as log:
        cuda = subprocess.Popen([str(root / 'calibrate')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log, text=True)
        children.append(cuda)
        assert receive(cuda) == 'ready'
        for case in ('idle', 'one', 'four'):
            with (root / (case + '.stderr')).open('w') as err:
                p = subprocess.Popen(['sudo', '-n', str(probe)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=err, text=True)
                children.append(p)
                ready = json.loads(receive(p)); assert ready['event'] == 'ready'
                send(p, 'start'); start = json.loads(receive(p))
                assert start['event'] == 'started'
                send(cuda, case); assert receive(cuda) == 'done'
                send(p, 'stop')
                # stop emits two lines, drain via communicate to avoid TextIO
                # buffering masking readiness of the second line.
                out, _ = p.communicate(timeout=10)
                stop, snapshot = [json.loads(x) for x in out.splitlines()]
                assert p.returncode == 0 and stop['event'] == 'stopped'
                assert snapshot['valid']
                rows.append({'case': case, 'start': start, 'stop': stop,
                             'snapshot': snapshot})
                (root / 'raw.json').write_text(json.dumps(rows, indent=2)+'\n')
                send(cuda, 'verify'); assert receive(cuda) == 'verified'
        assert cuda.wait(timeout=10) == 0
    byte_counts = [r['snapshot']['events'][1]['value'] for r in rows]
    ratio = byte_counts[2] / byte_counts[1] if byte_counts[1] else None
    passed = (byte_counts[1] > 128 * 1024**2 and
              byte_counts[1] > 10 * byte_counts[0] and
              ratio is not None and 2 <= ratio <= 6)
    review = {'raw_read_byte_event': byte_counts, 'four_over_one': ratio,
              'frozen_gate_passed': passed, 'performance_acceptance': False,
              'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (probe, root / 'calibrate')}}
    (root / 'review.json').write_text(json.dumps(review, indent=2)+'\n')
    print(json.dumps(review))
finally:
    for p in children:
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
