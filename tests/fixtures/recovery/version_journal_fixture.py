"""CPU-only root fixture: real version journal, synthetic qualification reply.

This is never a release module. Package/profile preparation has separate real
journal tests; here all payloads are idle CPU actors and the device probe is a
CPU executable, not evidence of CUDA usability or model service acceptance.
"""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from tools.serve_recovery import version_journal, version_runtime


def prepare(binding,selected,args):
    if args['payload'] != version_journal.policy.launch_payload(selected):
        raise ValueError('CPU fixture wrong selection')
    expected=selected['registry']['payloads'][args['payload']]
    return dict(payload=args['payload'],manifest_sha256=expected['manifest_sha256'],
        binding_sha256=expected['binding_sha256'],profile_registry_sha256='9'*64,
        profile_revision=1)


if __name__=='__main__':
    version_runtime.prepare=prepare
    raise SystemExit(version_journal.main())
