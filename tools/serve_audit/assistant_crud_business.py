"""Run typed Assistants v2 clients against one isolated native HTTPS service."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from tools.serve_audit.assistant_crud_profile import make_profile, verify


def exercise(sdk_paths, source, *, base, ca, owner, other, output):
    if not owner or not other or owner == other:
        raise ValueError('Two distinct authenticated clients are required')
    steps = make_profile(source)
    seen_traces = set()
    seen_ids = set()
    proof = {}
    config = dict(steps=steps, base=base, ca=str(ca), key=owner, other=other)
    for label, sdk in sdk_paths.items():
        actor = Path(__file__).with_name('assistant_crud.py')
        if label.startswith('python-'):
            cmd = [sdk, '-I', '-B', actor]
        elif label.startswith('node-'):
            cmd = ['node', actor.with_suffix('.mjs'), sdk]
        else:
            raise ValueError('Unknown SDK family: '+label)
        result = subprocess.run(cmd, input=json.dumps(config).encode(), timeout=60,
                                env=dict(os.environ, NODE_EXTRA_CA_CERTS=str(ca)),
                                capture_output=True)
        if result.returncode:
            error = result.stderr.decode(errors='replace').replace(owner, '[owner]').replace(other, '[other]')
            raise RuntimeError(label + ': ' + error[-1600:])
        data = json.loads(result.stdout)
        path = output / (label + '.json')
        with path.open('xb') as f:
            f.write(result.stdout)
        traces = verify(data, label, source)
        ids = set(data['ids'].values())
        if traces & seen_traces or ids & seen_ids:
            raise ValueError('Reused trace or object ID across SDKs')
        seen_traces.update(traces)
        seen_ids.update(ids)
        proof[label] = hashlib.sha256(result.stdout).hexdigest()
    return dict(sdk_wire_sha256=proof, requests=len(seen_traces),
                source_sha256=hashlib.sha256(source).hexdigest(),
                per_sdk_requests=len(steps), verified_objects=len(seen_ids))
