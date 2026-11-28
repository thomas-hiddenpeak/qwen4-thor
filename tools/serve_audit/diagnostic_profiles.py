"""Check that each managed capacity still delivers traceable serve diagnostics."""
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import time

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_service import package


ROOT = Path(__file__).resolve().parents[2]
KEY = 'standalone_diagnostic_output'
RECEIPT = 'serve-diagnostic-output.json'
PROOF = ROOT / 'docs/evidence/serve-diagnostic-pipe-20261128/device.json'


def prepare(out, build, deployment):
    from tools.serve_responses import environment_lifecycle_profiles as lifecycle

    proof = package.read(PROOF)
    passed = proof['result']
    package.require(proof['engine'] == package.sha(build / 'q4t') and
                    passed['stalled_wchan'] == 'pipe_write' and
                    passed['reader_closed'] and passed['same_process_ready'] and
                    passed['answer_while_full'] == passed['answer'] == '42',
                    'missing tested candidate or same-generation sink recovery')
    lifecycle.prepare_from_manifest(
        out, build, deployment, proof['predecessor_manifest'],
        reuse.UPLOAD_COMPLETION_REVIEWED, KEY, [Path(__file__), PROOF])


def log_line(row):
    error = row['body']['error']
    code = error['code']
    package.require(row['status'] == 400 and error['param'] == 'temperature' and
                    isinstance(code, str) and re.fullmatch(r'[a-zA-Z0-9_]+', code) and
                    re.fullmatch(r'[a-zA-Z0-9._:-]{1,128}', row['trace']),
                    'missing native parameter error or request identity')
    return f"[q4t][http] trace_id={row['trace']} status=400 error={code}\n".encode()


def check(row, identity, log):
    expected = log_line(row)
    offset = row['log_offset']
    package.require(row['pid'] == identity['pid'] and
                    row['start_ticks'] == identity['start_ticks'] and
                    row['pid'] > 0 and row['start_ticks'] > 0 and
                    type(offset) is int and offset >= 0 and
                    row['producer_nonblocking'] is True and
                    re.fullmatch(r'socket:\[\d+\]', row['stderr']),
                    'diagnostic model, producer or offset differs')
    with log.open('rb') as stream:
        stream.seek(offset)
        package.require(stream.read(len(expected)) == expected,
                        'request did not reach the original log destination')


def business(stage):
    stage.same_model()
    pid = stage.model.pid
    fd = Path('/proc', str(pid), 'fd', '2').readlink()
    info = Path('/proc', str(pid), 'fdinfo', '2').read_text()
    flags = int(re.search(r'^flags:\s+([0-7]+)$', info, re.MULTILINE)[1], 8)
    path = stage.out / 'model.log'
    offset = path.stat().st_size
    context = ssl.create_default_context(cafile=stage.plan['tls']['certificate'])
    conn = http.client.HTTPSConnection('127.0.0.1', stage.plan['public_port'],
                                       context=context, timeout=8)
    try:
        request = dict(model='qwen3.8-flash-next', temperature=-1,
                       messages=[dict(role='user', content='check validation')])
        conn.request('POST', '/v1/chat/completions', json.dumps(request),
                     {'Authorization': 'Bearer ' + stage.keys['alpha'],
                      'Content-Type': 'application/json'})
        reply = conn.getresponse()
        raw = reply.read(65537)
        package.require(len(raw) <= 65536, 'oversized diagnostic response')
        row = dict(pid=pid, start_ticks=stage.model_identity['start_ticks'],
                   stderr=str(fd), producer_nonblocking=bool(flags & os.O_NONBLOCK),
                   status=reply.status, trace=reply.getheader('x-request-id') or '',
                   body=json.loads(raw))
    finally:
        conn.close()
    expected = log_line(row)
    deadline = time.monotonic() + 3
    with path.open('rb') as stream:
        while True:
            stream.seek(offset)
            data = stream.read(65536)
            pos = data.find(expected)
            if pos >= 0:
                row['log_offset'] = offset + pos
                break
            package.require(len(data) < 65536 and time.monotonic() < deadline,
                            'request diagnostic absent from model log')
            time.sleep(.02)
    stage.same_model()
    check(row, package.read(stage.out / 'model-identity.json'), path)
    package.save(stage.out / RECEIPT, row)


def verify(phase, plan, capabilities, current):
    name = plan['name']
    previous_file = phase / 'previous-profile.json'
    previous = profile_record.read(previous_file)
    change = plan[KEY]
    proof = package.read(PROOF)
    package.require(package.sha(previous_file) == change['previous'] ==
                    reuse.UPLOAD_COMPLETION_REVIEWED[name] and
                    proof['predecessor_manifest'] == change['release'] ==
                    plan['selected_sha256'] and proof['engine'] == plan['binary_sha256'] and
                    plan['capacity'] == previous['capacity'] and
                    all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p)
                        for p in (PROOF, Path(__file__))) and
                    package.same_json(capabilities, dict(previous['capabilities'],
                                      protocol_source_sha256=change['protocol'])),
                    'diagnostic qualification, effective capability or source changed')
    observed = package.read(phase / RECEIPT)
    expected_error = proof['requests'][0]['body']['error']
    package.require(all(observed['body']['error'][key] == expected_error[key]
                        for key in ('type', 'code', 'param')),
                    'native rejection differs from the tested request')
    check(observed, package.read(phase / 'model-identity.json'), phase / 'model.log')
    inherited = previous['business']['live']['evidence_sha256']
    package.require(reuse.UPLOAD_COMPLETION_RECEIPT in inherited,
                    'missing previous Uploads qualification')
    return {**{k: v for k, v in inherited.items() if k not in current},
            RECEIPT: package.sha(phase / RECEIPT)}
