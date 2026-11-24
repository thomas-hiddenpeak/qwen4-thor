"""Exercise Evals definition CRUD through the pinned client and a CPU HTTP fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
ACTOR = ROOT / 'tools/serve_responses/evals_sdk.mjs'
OPERATIONS = {
    'create': ('/evals', 'post', 201),
    'retrieve': ('/evals/{eval_id}', 'get', 200),
    'update': ('/evals/{eval_id}', 'post', 200),
    'list': ('/evals', 'get', 200),
    'delete': ('/evals/{eval_id}', 'delete', 200),
}


def exercise(binary, sdk, spec_path, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    published = json.loads(spec_path.read_bytes())
    with (output / 'fixture.log').open('wb') as log:
        process = subprocess.Popen([binary, '--evals-api-fixture', output],
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            ready = output / 'ready.json'
            deadline = time.monotonic() + 8
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            receipt = json.loads(ready.read_bytes())
            assert receipt['pid'] == process.pid
            base = 'http://127.0.0.1:' + str(receipt['port'])
            settings = dict(base=base+'/v1', key='cpu-client-a',
                            other='cpu-client-b', sdk=str(sdk))
            with (output / 'sdk.stderr').open('wb') as errors:
                result = subprocess.run(['node', ACTOR], input=json.dumps(settings).encode(),
                                        stdout=subprocess.PIPE, stderr=errors, timeout=60, check=True)
            (output / 'sdk.json').write_bytes(result.stdout)
            with urlopen(Request(base+'/__fixture/stop', method='POST', data=b'',
                                 headers={'Authorization': 'Bearer cpu-client-a'}), timeout=5) as stop:
                assert stop.status == 200
            assert process.wait(timeout=8) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    facts = json.loads(result.stdout)
    assert facts['sdk'] == '7.47.0' and facts['cross_project'] == facts['deleted_read'] == 404
    assert [call['operation'] for call in facts['calls']] == list(OPERATIONS)
    for row in facts['calls']:
        path, verb, status = OPERATIONS[row['operation']]
        assert row['status'] == status
        schema = published['paths'][path][verb]['responses'][str(status)]['content']['application/json']['schema']
        validator = Draft202012Validator({**schema, 'components': published['components']})
        validator.validate(row['value'])
    result = dict(status='PASS', methods=len(OPERATIONS), validated_responses=len(facts['calls']),
                  sdk=facts['sdk'], spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
                  cross_project=404, model_starts=0)
    (output / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'sdk', 'spec', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(exercise(args.binary.resolve(strict=True), args.sdk.resolve(strict=True),
                              args.spec.resolve(strict=True), args.output.resolve())))
