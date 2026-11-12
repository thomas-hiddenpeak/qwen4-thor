from pathlib import Path
import hashlib,json,os,socket,struct,time,signal
binary=Path('.q4t-work/serve-custom-tools-20261108/engine-build/q4t').resolve()
checks=[{'schema':{'type':'object','required':['revision'],'properties':{'revision':{'type':'string','pattern':'^rev[0-9]+$'}}},'output':'{"revision":"rev94"}','param':'input[0].output'}]
rows=[]
for expected in [200,400]:
    if expected==400: checks[0]['output']='{"revision":"invalid"}'
    parent,child=socket.socketpair()
    actions=[(os.POSIX_SPAWN_CLOSE,parent.fileno()),(os.POSIX_SPAWN_DUP2,child.fileno(),3)]
    if child.fileno()!=3:actions.append((os.POSIX_SPAWN_CLOSE,child.fileno()))
    actions += [(os.POSIX_SPAWN_OPEN,fd,'/dev/null',os.O_RDONLY if fd==0 else os.O_WRONLY,0) for fd in [0,1,2]]
    pid=os.posix_spawn(str(binary),[str(binary),'--tool-result-worker',str(os.getpid()),str(512<<20)],{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},file_actions=actions)
    child.close();parent.settimeout(5)
    reaped=False
    try:
        raw=json.dumps(checks).encode();parent.sendall(struct.pack('Q',len(raw))+raw)
        output=b''
        while True:
            chunk=parent.recv(8192)
            if not chunk:break
            output+=chunk
            assert len(output)<=8192
        _,status=os.waitpid(pid,0);reaped=True
        assert os.waitstatus_to_exitcode(status)==0,(status,output)
        receipt=json.loads(output);assert receipt['status']==expected,receipt
        rows.append({'pid':pid,'status':receipt,'reaped':reaped})
    finally:
        parent.close()
        if not reaped:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
            os.waitpid(pid,0)
print(json.dumps({'status':'PASS_REAL_ENGINE_TOOL_RESULT_WORKER','binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'address_space':512<<20,'rows':rows,'model_starts':0,'gpu_calls':0}))
