"""Fixed passive adapter-0 trace; no I2C IO or GPU request is issued."""
import json
import os
from pathlib import Path
import re
import select
import signal
import time

out=Path(__file__).resolve().parent
instance=Path('/sys/kernel/tracing/instances')/f'q4t_i2c_{os.getpid()}'
assert not instance.exists()
instance.mkdir()
fd=None
identities={}
status={'instance':str(instance),'duration_seconds':300,'events':['i2c_read','i2c_write','i2c_result'],'filter':'adapter_nr == 0','clock':'mono','complete':False}
def interrupted(signum,frame):raise InterruptedError(signum)
signal.signal(signal.SIGTERM,interrupted)
signal.signal(signal.SIGINT,interrupted)
try:
    (instance/'tracing_on').write_text('0')
    (instance/'trace_clock').write_text('mono')
    (instance/'buffer_size_kb').write_text('64')
    for event in status['events']:
        path=instance/'events/i2c'/event
        (out/(event+'-format.txt')).write_text((path/'format').read_text())
        (path/'filter').write_text(status['filter'])
        (path/'enable').write_text('1')
    fd=os.open(instance/'trace_pipe',os.O_RDONLY|os.O_NONBLOCK)
    status['start_ns']=time.monotonic_ns()
    (instance/'tracing_on').write_text('1')
    (out/'started.json').write_text(json.dumps(status,indent=2)+'\n')
    pending=''
    with (out/'trace.txt').open('w') as stream:
        while time.monotonic_ns()-status['start_ns']<300_000_000_000:
            ready,_,_=select.select([fd],[],[],0.5)
            if not ready:continue
            pending+=os.read(fd,65536).decode(errors='replace')
            lines=pending.split('\n');pending=lines.pop()
            for line in lines:
                stream.write(line+'\n')
                match=re.search(r'-(\d+)\s+\[',line)
                if not match:continue
                tid=match[1]
                if tid in identities:continue
                proc=Path('/proc')/tid
                try:
                    identities[tid]={'comm':(proc/'comm').read_text().strip(),
                        'cmdline':(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace'),
                        'stat':(proc/'stat').read_text().strip(),
                        'observed_ns':time.monotonic_ns()}
                except OSError as error:identities[tid]={'unavailable':repr(error)}
            stream.flush()
        (instance/'tracing_on').write_text('0')
        # Drain only the stopped trace, not another observation interval.
        while True:
            try:chunk=os.read(fd,65536)
            except BlockingIOError:break
            if not chunk:break
            stream.write(chunk.decode(errors='replace'))
        if pending:stream.write(pending)
    status['end_ns']=time.monotonic_ns()
    status['cpu_stats']={p.parent.name:p.read_text() for p in (instance/'per_cpu').glob('cpu*/stats')}
    status['complete']=True
finally:
    (instance/'tracing_on').write_text('0')
    for event in status['events']:(instance/'events/i2c'/event/'enable').write_text('0')
    if fd is not None:os.close(fd)
    instance.rmdir()
    status['instance_removed']=not instance.exists()
    (out/'callers.json').write_text(json.dumps(identities,indent=2)+'\n')
    (out/'exit.json').write_text(json.dumps(status,indent=2)+'\n')
print(json.dumps({'complete':status['complete'],'callers':identities,'instance_removed':status['instance_removed']}),flush=True)
