"""Three bounded real loopback requests using a sealed package transport."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

sys.dont_write_bytecode=True
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--package',type=Path,required=True)
p.add_argument('--digest',required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--parent',type=int,required=True)
args=p.parse_args()
sys.path.insert(0,str(args.package/'source'))
from tools.serve_service import package as pkg,supervisor as sup
from tools.serve_ops import transport
from tools.serve_ops.contracts import InvalidObservation

os.umask(0o077)
sup.arm_parent_death(sup.process_identity(args.parent))
pkg.verify(args.package,args.digest)
args.output.mkdir(mode=0o700)
rows=[]
for mode in ('cancel-no-response','absolute-drip-deadline','healthy-after-refusal'):
    stop=threading.Event();done=threading.Event();entered=threading.Event();errors=[]
    listener=socket.socket();listener.settimeout(2)
    listener.bind(('127.0.0.1',0));listener.listen(1)
    port=listener.getsockname()[1]
    def peer():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                raw=b''
                while b'\r\n\r\n' not in raw:
                    part=connection.recv(4096)
                    if not part:raise RuntimeError('request disappeared')
                    raw+=part
                    pkg.require(len(raw)<=16384,'fixture request too large')
                entered.set()
                if mode=='cancel-no-response':
                    done.wait(1)
                elif mode=='absolute-drip-deadline':
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}':
                        if done.wait(.03):break
                        connection.sendall(bytes([byte]))
                else:
                    connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}')
        except (BrokenPipeError,ConnectionResetError):
            if not done.is_set():errors.append('unexpected peer reset')
        except BaseException as error:errors.append(repr(error))
        finally:listener.close()
    thread=threading.Thread(target=peer);thread.start()
    timer=None
    def cancel():
        if entered.wait(1):
            time.sleep(.1);stop.set()
    if mode=='cancel-no-response':
        timer=threading.Thread(target=cancel);timer.start()
    started=time.monotonic();value=None;reason=None
    try:
        try:
            value=transport.get(port,'/livez',timeout=1,
                deadline=started+.15 if mode=='absolute-drip-deadline' else started+1,
                stopped=stop.is_set)
        except InvalidObservation as error:reason=error.code
        elapsed=time.monotonic()-started
    finally:
        done.set();thread.join(3)
        if timer is not None:timer.join(2)
        listener.close()
    pkg.require(not thread.is_alive() and not errors,'peer cleanup/error: '+repr(errors))
    expected={'cancel-no-response':'observation_stopped','absolute-drip-deadline':'http_deadline','healthy-after-refusal':None}[mode]
    pkg.require(reason==expected and elapsed<.7,'transport outcome/deadline')
    if value is not None:pkg.require(value['http_status']==200 and value['body']=='{}','healthy response')
    rows.append(dict(mode=mode,seconds=elapsed,reason=reason,response=value,port=port,peer_closed=True))
pkg.save(args.output/'result.json',dict(passed=True,requests=3,models=0,rows=rows,
    transport_sha256=pkg.sha(transport.__file__)))
print(json.dumps(dict(passed=True,requests=3)))
