"""Check real SDK retry behavior with a loopback entrypoint and local CPU peers."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import subprocess
import sys

from tools.serve_recovery import test_gateway_nginx as gateway, test_native_ingress as native
from tools.serve_service import package


def answer(handler, body, generation):
    if body.get('model') != 'cpu-contract':
        return False
    choice = dict(index=0, finish_reason='stop', message=dict(role='assistant', content='ok'))
    handler.response(200, dict(id='chatcmpl-cpu', created=1, model=body['model'],
                               object='chat.completion', choices=[choice]))
    return True


def one_case(argv, version, language, status, accepted, out):
    case = native.NativeTests()
    case.setUp()
    try:
        case.replace_stack(per_client_inflight=1, global_inflight=1)
        stack = case.stack
        # The peer reads and records the POST, then closes without sending headers.
        stack.response_hook = (lambda *_: True) if accepted else answer
        with ExitStack() as resources:
            if status == 429:
                sock, response = case.stream()
                resources.callback(sock.close)
                resources.callback(response.close)
                resources.callback(stack.release_stream.set)
            elif not accepted:
                stack.ready = False
            before = len(stack.requests)
            port = stack.value['policy']['listen_port']
            settings = dict(status=status, accepted=accepted,
                            base=f'http://127.0.0.1:{port}/v1', key='A' * 40)
            log = resources.enter_context((out / f'{language}-{version}-{status}-{accepted}.log').open('x'))
            proc = resources.enter_context(subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True))
            resources.callback(lambda: proc.kill() if proc.poll() is None else None)
            proc.stdin.write(json.dumps(settings) + '\n')
            proc.stdin.flush()
            if not accepted:
                with DefaultSelector() as selector:
                    selector.register(proc.stdout, EVENT_READ)
                    if not selector.select(12):
                        raise TimeoutError('SDK never received the pre-dispatch rejection')
                    assert json.loads(proc.stdout.readline()) == {'rejected': status}
                assert len(stack.requests) == before
                if status == 429:
                    stack.release_stream.set()
                    response.read()
                    response.close()
                    sock.close()
                else:
                    stack.ready = True
            stdout, _ = proc.communicate(None if accepted else 'ready\n', timeout=12)
            if proc.returncode:
                raise RuntimeError(f'SDK client failed; see {log.name}')
            result = json.loads(stdout)
            expected = [status] if accepted else [status, status, 200]
            assert result['version'] == version and result['attempts'] == expected
            assert result['reply'] == (None if accepted else 'ok')
            err = result['error']
            assert err['status'] == status and err['param'] is None
            assert re.fullmatch('[A-Za-z0-9_.:-]{1,128}', err['trace'])
            assert err['trace'] == err['headerTrace']
            assert err['delay'] == (None if accepted else '1')
            assert err['retry'] == ('false' if accepted else None)
            assert err['name'] == ('RateLimitError' if status == 429 else 'InternalServerError')
            assert err['type'] == ('gateway_error' if status == 429 else 'service_error')
            if not accepted:
                assert err['code'] == ('gateway_rate_limit' if status == 429 else 'service_recovering')
            assert len(stack.requests) == before + 1 and stack.requests[-1]['body']['model'] == 'cpu-contract'
            return dict(language=language, version=version, status=status, accepted_before_error=accepted,
                        attempts=result['attempts'], backend_submissions=1, trace_verified=True,
                        error_class=err['name'], type=err['type'], code=err['code'],
                        retry_after=err['delay'], should_retry=err['retry'])
    finally:
        case.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--native', type=Path)
    group.add_argument('--nginx', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--python-sdk', nargs=2, action='append', default=[],
                        metavar=('VERSION', 'PYTHON'))
    parser.add_argument('--node-sdk', nargs=2, action='append', default=[],
                        metavar=('VERSION', 'PACKAGE'))
    args = parser.parse_args()
    if not (args.python_sdk or args.node_sdk):
        parser.error('supply at least one installed SDK')
    directory = Path(__file__).parent
    actors = [('python', version, [exe, '-IB', directory/'retry_client.py'])
              for version, exe in args.python_sdk]
    actors.extend(('node', version, ['node', directory/'retry_client.mjs', path])
                  for version, path in args.node_sdk)
    out = args.output.resolve()
    out.mkdir(mode=0o700)
    os.environ['Q4T_TEST_OUTPUT_DIR'] = str(out)
    binary = (args.native or args.nginx).resolve(strict=True)
    if args.native:
        native.INGRESS = binary
    else:
        gateway.NGINX = binary
    native.NativeTests.setUpClass()
    try:
        rows = [one_case(argv, version, language, status, accepted, out)
                for language, version, argv in actors
                for status, accepted in [(429, False), (503, False), (503, True)]]
        package.save(out/'result.json', dict(model_starts=0, gateway_sha256=package.sha(binary),
                     gateway='native' if args.native else 'nginx', cases=rows))
    finally:
        native.NativeTests.tearDownClass()


if __name__ == '__main__':
    main()
