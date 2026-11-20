"""CPU-only preparation subtree, including a separate process session."""
import json
import os
from pathlib import Path
import subprocess
import sys


def identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]))


directory, payload, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
    'import time;time.sleep('+('300' if mode == 'hold' else '.4')+')'],
    start_new_session=True)
record = dict(worker=identity(os.getppid()), child=identity(os.getpid()),
              grandchild=identity(child.pid), payload=payload)
path = directory/('prepare-children-'+payload+'.json')
path.write_text(json.dumps(record))
result = child.wait(timeout=310)
record.update(exit_code=result)
path.write_text(json.dumps(record))
raise SystemExit(result)
