"""Run six stock SDKs against an already running private q4t TLS endpoint."""
import json
import os
from pathlib import Path
import subprocess
import time

from .container_receipt import verify
from .render_sdk_matrix import VERSIONS


def run(out, base, keys, certificate, sdk_paths, document):
    versions = {f'{language}-{version}' for language, version in VERSIONS}
    if sdk_paths.keys() != versions:
        raise ValueError('Expected all six registered SDK versions')
    out.mkdir(mode=0o700)
    records = {}
    traces = set()
    data = document.read_bytes()
    for label, sdk in sdk_paths.items():
        if records:
            # The next 15-call business flow shares the previous owner's 20-RPS key.
            time.sleep(1)
        python = label.startswith('python-')
        actor = Path(__file__).with_name('container_business.' + ('py' if python else 'mjs'))
        args = [sdk, '-I', '-B', actor] if python else ['node', actor, sdk]
        config = dict(base=base, key=keys['alpha'], peer=keys['beta'],
                      document=str(document), certificate=str(certificate))
        response = subprocess.run(args, input=json.dumps(config).encode(), capture_output=True,
            timeout=75, env=dict(os.environ, NODE_EXTRA_CA_CERTS=str(certificate)))
        diagnostic = response.stderr.decode(errors='replace')
        for key in keys.values():
            diagnostic = diagnostic.replace(key, '<redacted>')
        (out/(label+'.log')).write_text(diagnostic)
        if response.returncode:
            raise RuntimeError(f'{label} failed; see the actor log')
        result = json.loads(response.stdout)
        (out/(label+'.json')).write_bytes(response.stdout)
        observed = verify(label, result, data)
        if not traces.isdisjoint(observed):
            raise ValueError(f'{label}: request ID reused across clients')
        traces.update(observed)
        records[label] = result
    return records, traces
