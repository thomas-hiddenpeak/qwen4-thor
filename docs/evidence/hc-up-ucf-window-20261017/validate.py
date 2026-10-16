import json
from pathlib import Path
import subprocess
import time
root = Path(__file__).resolve().parent
results = []
for case in ('window', 'bad_command', 'eof'):
    p = subprocess.Popen(['sudo', '-n', str(root / 'probe')], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    rows = []
    def receive():
        row = json.loads(p.stdout.readline())
        rows.append(row)
        return row
    assert receive()['event'] == 'ready'
    p.stdin.write('start\n'); p.stdin.flush()
    assert receive()['event'] == 'started'
    if case == 'window':
        time.sleep(.02)
        p.stdin.write('read\n'); p.stdin.flush()
        first = receive()
        time.sleep(.02)
        p.stdin.write('stop\n'); p.stdin.flush()
        assert receive()['event'] == 'stopped'
        last = receive()
        assert first['valid'] and last['valid']
        assert last['enabled_ns'] > first['enabled_ns'] > 0
        assert first['end_ns'] < last['begin_ns']
        assert [x['id'] for x in first['events']] == [x['id'] for x in last['events']]
        assert all(b['value'] >= a['value'] for a,b in zip(first['events'], last['events']))
        expected = 0
    elif case == 'bad_command':
        p.stdin.write('start\n'); p.stdin.flush()
        assert receive()['error'] == 'expected_read_or_stop'
        expected = 2
    else:
        p.stdin.close()
        assert receive()['error'] == 'unexpected_eof'
        expected = 2
    rc = p.wait(timeout=5)
    assert rc == expected, (rc, p.stderr.read())
    results.append({'case': case, 'exit_code': rc, 'rows': rows})
(root / 'validation.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps({'passed': len(results), 'model_workload': False}))
