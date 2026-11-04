"""Actual supervised CPU fault process; no model, listener or HTTP observation."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resources as r,package as pkg,supervisor as sup
from tools.serve_ops import contracts


def completed(output,case):
    if case=='normal':return (output/'payload-held.json').exists()
    if case=='high':
        # The kernel may throttle allocation for longer than this short probe.
        # Its actual high event, not completion of an oversized bytearray,
        # is the bounded case's success boundary.
        value=r.sample(pkg.read(output/'resources.json'))
        return value['memory.events']['high']>0
    return False


def main():
    os.umask(0o077)
    output,case=Path(sys.argv[1]),sys.argv[2]
    pkg.save(output/'worker-owner.json',sup.process_identity(os.getpid()))
    cfg=pkg.settings(18997,3)
    cfg.update(poll_seconds=.05,startup_seconds=12,unavailable_seconds=12,stop_seconds=2,kill_seconds=2)
    selected=r.policy(32 if case=='high' else 64,64,8 if case=='tasks' else 32)
    manifest=dict(settings=cfg,repository=str(ROOT),protocol_sha256='cpu-only',files={'q4t':'cpu-only'},host_resources=selected)
    argv=[sys.executable,'-B',str(ROOT/'tools/serve_service/resource_payload.py'),str(output),case]
    def observe(pid,port):
        if not (output/'payload-ready.json').exists():
            raise contracts.InvalidObservation('cpu_starting')
        if not (output/'go.json').exists():pkg.save(output/'go.json',dict(ready_observed=True))
        if completed(output,case):
            pkg.save(output/'stop.request.json',dict(reason='bounded_cpu_completed'))
        return dict(process=dict(pid=pid,executable_sha256='cpu-only',listener_scope='loopback'),
            protocol='cpu-only',ready=True,metrics={'q4t_gpu_healthy':1},configuration=sup.expected_configuration())
    with patch.object(pkg,'verify',return_value=manifest),patch.object(pkg,'command',return_value=argv),\
        patch.object(sup,'backend_command',return_value=argv):
        code=sup.run(ROOT,'cpu-only',output,observe=observe,
            read_memory=lambda:dict(total_bytes=8*1024**3,available_bytes=4*1024**3))
    pkg.save(output/'worker-exit.json',dict(returncode=code,case=case))
    return code


if __name__=='__main__':raise SystemExit(main())
