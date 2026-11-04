"""Offline source-policy receipts, wire reconstruction and resource closeout."""
import argparse
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as pkg
from tools.examples import chat_client

BASE = ROOT/'.q4t-work/serve-gateway-sources-20261104'
DEST = ROOT/'docs/evidence/serve-gateway-sources-20261104'
READERS = ('tools/serve_gateway/sources_report.py', 'tools/examples/chat_client.py')
require = pkg.require
sha = lambda raw: hashlib.sha256(raw).hexdigest()


def frozen_plan():
    """Compare executed sources to their snapshot; only offline readers changed."""
    from tools.serve_gateway import managed as m
    plan = pkg.read(BASE/'plan.json')
    repairs = {'tools/serve_gateway/sources_stage.py', 'tools/serve_gateway/sources_report.py'}
    for name, digest in plan['source_files'].items():
        path = BASE/'source'/name if name in repairs else ROOT/name
        require(pkg.sha(path) == digest, 'frozen execution source changed: '+name)
    for name, digest in plan['runtime'].items():
        require(pkg.sha(ROOT/name) == digest, 'model runtime changed')
    for name, digest in {**plan['preserved'], **plan['default'], **plan['local']}.items():
        require(pkg.sha(name) == digest, 'preserved input changed')
    for name, digest in plan['model_inputs'].items():
        require(pkg.sha(Path(plan['model_dir'])/name) == digest, 'model input changed')
    for name in plan['days']:
        require(not (Path(name).parent/'execution').exists(), 'day executed')
    outer, _ = m.verify(plan['managed_gateway']['package'], plan['managed_gateway']['manifest_sha256'])
    require(outer['gateway'] == plan['gateway'], 'gateway identity')
    return plan


def postflight():
    from tools.serve_gateway import sources_stage as s
    from tools.serve_cuda_quota import correction as custody
    plan = frozen_plan()
    out = BASE/'postflight'
    out.mkdir(mode=0o700)
    s.s.a.require_idle(out/'gpu.json')
    lock = s.s.a.guard.common_lock()
    info = lock.stat()
    require((info.st_dev, info.st_ino) == (66305, 3294067) and
            s.s.a.lock_available(lock), 'original GPU lock not released')
    owners = []
    for folder in ('scoped-model', 'nginx'):
        old = pkg.read(BASE/'execution/business'/folder/'identity.json')
        try:
            current = custody.proc(old['pid'])
        except FileNotFoundError:
            current = None
        require(current is None or current['start_ticks'] != old['start_ticks'], 'own process remains')
        owners.append(dict(pid=old['pid'], start_ticks=old['start_ticks'], gone=True))
    for port in (s.PORT, s.TLS_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
    listeners = subprocess.check_output(['ss', '-ltn',
        f'( sport = :{s.PORT} or sport = :{s.TLS_PORT} )'], text=True)
    (out/'listeners.txt').write_text(listeners)
    require(len(listeners.splitlines()) == 1, 'listener remains')
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    state = subprocess.check_output(['systemctl', '--user', 'show', unit, '-p',
        'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'], text=True)
    (out/'soar-unit.txt').write_text(state)
    require(dict(line.split('=', 1) for line in state.splitlines()) ==
        dict(LoadState='masked', ActiveState='inactive', SubState='dead', MainPID='0'),
        'recovery unit state changed')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '--format',
                                         '{{.ID}} {{.Names}}'], text=True)
    (out/'containers.txt').write_text(containers)
    require(not containers.strip(), 'unexpected container')
    pkg.save(out/'result.json', dict(passed=True, owned=owners,
        lock=dict(dev=info.st_dev, inode=info.st_ino, released=True),
        ports_released=[s.PORT, s.TLS_PORT], preserved=plan['preserved'],
        default=plan['default'], runtime=plan['runtime'], days_unexecuted=plan['days'],
        new_model_starts=0, new_http=0, default_replaced=False))


def evaluate(data):
    read = lambda name: json.loads(data[name])
    plan = read('plan.json')
    first, fixed = [read(n+'/result.json') for n in ('cpu-01', 'cpu-syntax-fix')]
    require(not first['passed'] and first['tests_run'] == 78 and
        first['errors'] == 1 and first['failures'] == 0 and
        fixed['passed'] and fixed['tests_run'] == 7 and
        b'Cannot assign requested address' in data['original-syntax-failure.log'], 'original CPU/correction')
    for label, receipt in (('cpu-01', first), ('cpu-syntax-fix', fixed)):
        require(receipt['models_started'] == receipt['http_started'] == receipt['gpu_started'] == 0,
                'CPU started HTTP/model')
        require(sha(data[label+'/result.json']) == plan['local'][str(BASE/label/'result.json')],
                'CPU receipt binding')
        for name, digest in receipt['sources'].items():
            require(sha(data[label+'/source/'+name]) == digest, 'CPU source evidence')
            if name not in plan['source_files']:
                require(label == 'cpu-01' and name == 'tools/serve_service/test_service.py',
                        'unexpected CPU-only fixture')
            elif label != 'cpu-01' or name not in ('tools/serve_gateway/test_sources.py',
                                                  'tools/serve_gateway/sources_stage.py'):
                require(plan['source_files'][name] == digest, 'unexpected production source change')
    for name, digest in plan['source_files'].items():
        require(sha(data['source/'+name]) == digest, 'frozen source identity')
    outer, inner = read('gateway/manifest.json'), read('gateway/gateway/manifest.json')
    require(sha(data['gateway/manifest.json']) == plan['managed_gateway']['manifest_sha256'] and
            outer['gateway'] == plan['gateway'] and
            sha(data['gateway/gateway/manifest.json']) == plan['gateway']['manifest_sha256'] and
            (outer['version'], inner['version'], inner['policy']['version']) == (4, 3, 2) and
            inner['policy']['allowed_client_cidrs'] == ['127.0.0.1/32'], 'new fixed gateway policy')
    for name, digest in outer['sources'].items():
        require(plan['source_files'][name] == digest and
                sha(data['gateway/source/'+name]) == digest, 'managed source snapshot')
    for name in ('certificate.pem', 'trusted-ca.pem'):
        require(sha(data['gateway/gateway/'+name]) == inner['files'][name], 'public certificate identity')
    leaf = sha(ssl.PEM_cert_to_DER_cert(data['gateway/gateway/certificate.pem'].decode()))
    require(leaf == inner['tls']['chain'][0]['sha256'], 'leaf metadata')
    result = read('execution/business/result.json')
    require(result['status'] == 'FAILED' and result['cleanup_confirmed'] and
            result['error'] == "RuntimeError('refused request proxied')" and
            not result['long_run'] and not result['performance_acceptance'] and
            b'self.logs()' in data['driver.stderr'], 'original post-run assertion retained')
    regression = read('cpu-log-fix/result.json')
    require(regression['passed'] and regression['tests_run'] == 3 and
            regression['models_started'] == regression['http_started'] == regression['gpu_started'] == 0,
            'access log correction regression')
    for name, digest in regression['sources'].items():
        require(sha(data['cpu-log-fix/source/'+name]) == digest, 'correction source identity')
        if name in plan['source_files'] and name not in (
                'tools/serve_gateway/sources_stage.py', 'tools/serve_gateway/sources_report.py'):
            require(plan['source_files'][name] == digest, 'unrelated runtime change')
    require(result['counts']['starts'] == 1 and result['counts']['generation_attempts'] == 7 and
            all(result['counts'][k] <= plan['limits'][k] for k in result['counts']), 'attempt bounds')
    rows = [json.loads(line) for line in data['execution/business/nginx/stdout.log'].splitlines()]
    # The original post-run assertion failed before writing its derived view.
    # Reconstruct directly from the retained nginx log; do not invent that file.
    require(len(rows) == 25, 'gateway log request count')
    by_id = {v['gateway_id']: v for v in rows}
    require(len(by_id) == 25, 'gateway correlation uniqueness')
    for request in plan['matrix']:
        prefix = 'execution/business/source-'+request['name']
        require(read(prefix+'-request.json') == request, 'frozen probe request')
        response, wire = read(prefix+'-http.json'), read(prefix+'-wire.bin')
        require(response['source'] == request['source'] and response['status'] == request['expected'] and
                response['verified'] and response['leaf_sha256'] == leaf and
                response['tls_version'] in ('TLSv1.2', 'TLSv1.3') and response['seconds'] < 8,
                'real TCP source/TLS/status')
        if request['code'] is not None:
            require(wire['error']['code'] == request['code'], 'expected refusal code')
        row = by_id[response['headers']['x-q4t-gateway-request-id']]
        require(row['status'] == response['status'] and row['source_allowed'] ==
                response['source_allowed'] == int(request['expected'] != 403), 'source access evidence')
        if response['status'] in (401, 403, 404):
            require(row['upstream_status'] in ('', '-'), 'gateway rejection reached backend')
        if response['status'] == 403:
            require(response['headers'].get('cache-control') == 'no-store' and
                    'x-request-id' not in response['headers'] and wire['error']['param'] is None and
                    wire['error']['type'] == 'gateway_error', '403 framing')
    sources = read('execution/business/sources.json')
    for name in ('q4t_requests_total', 'q4t_requests_success_total',
                 'q4t_structured_workers_started_total', 'q4t_structured_workers_reaped_total'):
        require(sources['before'][name] == sources['after'][name] == 0, 'rejection started model execution')
    usage = {}
    model_document = data['source/docs/MODEL.md']
    for index, client in enumerate(('alpha', 'beta')):
        completions = {}
        for step in ('read', 'answer'):
            label = 'project-'+client+'-'+step
            prefix = 'execution/business/'+label
            request = read(prefix+'-request.json')
            receipt = read(prefix+'-http.json')
            value = chat_client.read_sse(io.BytesIO(data[prefix+'-wire.bin']))
            normalized = read(prefix+'-response.json')
            normalized.pop('elapsed')
            require(asdict(value) == normalized and receipt['status'] == 200 and
                request['client'] == index and request['body']['max_tokens'] == (512 if index == 0 else 64) and
                value.id == request['body']['request_id'] and value.model == request['body']['model'] and
                value.usage['completion_tokens'] <= request['body']['max_tokens'], 'SSE/identity/client budget')
            headers = {k.lower(): v for k, v in receipt['headers'].items()}
            row = by_id[headers['x-q4t-gateway-request-id']]
            require(row['source_allowed'] == 1 and row['client'] == client and row['status'] == 200 and
                    row['upstream_status'] == '200', 'real allowed client generation')
            completions[step] = value
            usage[label] = value.usage
        calls = completions['read'].message['tool_calls']
        require(len(calls) == 1 and calls[0]['function']['name'] == 'read_project_record' and
                json.loads(calls[0]['function']['arguments']) ==
                dict(path='docs/MODEL.md', start_line=1, line_count=24), 'real tool request')
        returned = read('execution/business/project-'+client+'-tool-result.json')
        value = json.loads(returned['content'])
        require(returned['tool_call_id'] == calls[0]['id'] and value['ok'] and
                value['source']['sha256'] == sha(model_document) and
                value['source']['text'] == '\n'.join(f'{i+1}: {line}' for i, line in
                    enumerate(model_document.decode().splitlines()[:24])), 'actual document result')
        answer = completions['answer']
        require(answer.finish_reason == 'stop' and 'Qwen4ExpForConditionalGeneration' in answer.message['content'] and
                value['source']['citation'] == '[docs/MODEL.md:L1-L24]' and
                value['source']['citation'] in answer.message['content'], 'final answer/citation')
    metrics = read('execution/business/final-metrics.json')
    require(metrics['q4t_requests_total'] == metrics['q4t_requests_success_total'] == 4 and
            metrics['q4t_structured_workers_started_total'] == metrics['q4t_structured_workers_reaped_total'] == 4,
            'model/grammar accounting')
    require(b'shutdown complete (0 in-flight remaining)' in data['execution/business/scoped-model/stderr.log'], 'drain')
    for name in ('scoped-model', 'nginx'):
        require(read('execution/business/'+name+'/reaped.json')['returncode'] == 0, 'child reap')
    events = [json.loads(line) for line in data['execution/lease/events.jsonl'].splitlines()]
    observed = [v for v in events if v['event'] in ('preflight', 'monitor')]
    require(events[0]['event'] == 'lease_acquired' and events[-1]['event'] == 'lease_released' and
            events[-1]['status'] == read('execution/lease/exit.json')['status'] == 'COMMAND_FAILED' and
            observed and all(not v['conflicts'] for v in observed) and
            all(0 <= b['monotonic']-a['monotonic'] <= 10 for a, b in zip(events, events[1:])), 'full lease')
    seconds = events[-1]['monotonic']-events[0]['monotonic']
    require(0 < seconds < plan['limits']['seconds'] and result['seconds'] < plan['limits']['seconds'], 'short bound')
    post = read('postflight/result.json')
    require(post['passed'] and len(post['owned']) == 2 and all(v['gone'] for v in post['owned']) and
            post['lock'] == dict(dev=66305, inode=3294067, released=True) and
            post['default'] == plan['default'] and post['runtime'] == plan['runtime'] and
            post['preserved'] == plan['preserved'] and post['days_unexecuted'] == plan['days'], 'preserved identity/cleanup')
    require(read('postflight/gpu.json')['status'] == 'OK' and not read('postflight/gpu.json')['pids'] and
            not data['postflight/containers.txt'].strip() and
            len(data['postflight/listeners.txt'].splitlines()) == 1, 'GPU/container/listener')
    require(dict(line.split('=', 1) for line in data['postflight/soar-unit.txt'].decode().splitlines()) ==
            dict(LoadState='masked', ActiveState='inactive', SubState='dead', MainPID='0'), 'SOAR state')
    return dict(status='ACCEPT_BOUNDED_GATEWAY_SOURCES', effective_cpu=78, final_driver_tests=6,
        first_cpu_error_preserved=True, models=1, gateways=1, tls_requests=25, source_refusals=16,
        successful_responses=4, real_tools=2, usage=usage, counts=result['counts'],
        gpu_seconds=seconds, guard_observations=len(observed), driver_seconds=result['seconds'],
        owned_processes_reaped=2, default=plan['default'], runtime_sources=len(plan['runtime']),
        access_log_regressions=3, original_postrun_assertion_preserved=True,
        original_guard_status='COMMAND_FAILED', acceptance_reconstructed_offline=True,
        actual_lan_deployment=False, new_managed_lifecycle=False, long_run=False,
        performance_acceptance=False, new_verification_http=0)


def archive():
    plan = frozen_plan()
    data = {'plan.json': (BASE/'plan.json').read_bytes(),
            'original-syntax-failure.log': (BASE/'original-syntax-failure.log').read_bytes()}
    for name in ('driver.stdout', 'driver.stderr', 'archive-01.stdout', 'archive-01.stderr'):
        data[name] = (BASE/name).read_bytes()
    for folder in ('cpu-01', 'cpu-syntax-fix', 'cpu-log-fix', 'source', 'execution', 'postflight'):
        for path in sorted((BASE/folder).rglob('*')):
            if path.is_file() and not any(part in ('sessions', 'prefixes') for part in path.parts):
                data[str(path.relative_to(BASE))] = path.read_bytes()
    for name in ('manifest.json', 'gateway/manifest.json', 'gateway/certificate.pem', 'gateway/trusted-ca.pem'):
        data['gateway/'+name] = (BASE/'gateway'/name).read_bytes()
    for path in sorted((BASE/'gateway/source').rglob('*')):
        if path.is_file():
            data[str(path.relative_to(BASE))] = path.read_bytes()
    for name in READERS:
        data['reader/'+name] = (ROOT/name).read_bytes()
    private = [c['api_key'].encode() for c in pkg.read(plan['frontend'])['clients']]
    private.append(pkg.read(plan['policy'])['gateway_token'].encode())
    for name, raw in data.items():
        require(len(raw) < 16*1024**2 and not any(secret in raw for secret in private) and
                not re.search(br'(?m)^-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----\r?$', raw),
                'secret/oversized record: '+name)
    result = evaluate(data)
    DEST.mkdir()
    with (DEST/'records.tar.gz').open('xb') as stream, \
         gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for name, raw in sorted(data.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(raw), 0o600, 0
                tar.addfile(info, io.BytesIO(raw))
    pkg.save(DEST/'result.json', result)
    pkg.save(DEST/'manifest.json', dict(archive_sha256=pkg.sha(DEST/'records.tar.gz'),
        result_sha256=pkg.sha(DEST/'result.json'),
        records={name: dict(bytes=len(raw), sha256=sha(raw)) for name, raw in sorted(data.items())},
        readers={name: pkg.sha(ROOT/name) for name in READERS},
        local_binaries={str(path): pkg.sha(path) for path in
            (ROOT/'build/q4t', BASE/'gateway/nginx', Path(sys.executable).resolve(), Path('/usr/bin/openssl'))}))
    print(dict(archived=len(data), bytes=(DEST/'records.tar.gz').stat().st_size, status=result['status']))


def verify():
    manifest = pkg.read(DEST/'manifest.json')
    require(all(pkg.sha(ROOT/name) == digest for name, digest in manifest['readers'].items()), 'reader source drift')
    require(pkg.sha(DEST/'records.tar.gz') == manifest['archive_sha256'] and
            pkg.sha(DEST/'result.json') == manifest['result_sha256'], 'archive/result identity')
    with tarfile.open(DEST/'records.tar.gz', 'r:gz') as tar:
        members = tar.getmembers()
        require(len(members) == len(manifest['records']) and all(m.isfile() and m.size < 16*1024**2 for m in members),
                'archive members')
        data = {m.name: tar.extractfile(m).read() for m in members}
    require(set(data) == set(manifest['records']), 'archive names')
    for name, raw in data.items():
        require(manifest['records'][name] == dict(bytes=len(raw), sha256=sha(raw)), 'record identity')
    require(evaluate(data) == pkg.read(DEST/'result.json'), 'offline result differs')
    require(all(pkg.sha(name) == digest for name, digest in manifest['local_binaries'].items()), 'local binary identity')
    print(dict(passed=True, records=len(data), local_binaries=len(manifest['local_binaries']), new_http=0, new_model=0))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('postflight', 'archive', 'verify'))
    globals()[parser.parse_args().action]()
