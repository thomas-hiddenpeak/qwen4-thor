"""Read-only audit and lossless archive of the complete integration history."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c, verify, continuation as fix

DEST = ROOT / 'docs/evidence/serve-cuda-quota-integration-20261104'


def evaluate(data):
    read = lambda name: json.loads(data[name])
    lines = lambda name: [json.loads(line) for line in data[name].splitlines()]
    original = read('cpu/tests/result.json')
    c.require(not original['passed'] and {name for name, ok in original['results'].items()
        if not ok} == {'zero_owner', 'host-off', 'host-required'}, 'original CPU failures')
    c.require(b'FAIL zero_owner' in data['cpu/tests/wrapper-zero_owner.stderr'],
              'original raw wrapper failure')
    for name, count in [('host-off.stdout', 268), ('host-gate/run.log', 234)]:
        raw = data['cpu/tests/' + name].decode()
        c.require(f'{count} tests, {count-1} passed, 1 failed, 0 skipped' in raw and
            '[FAIL] cuda_quota_options_default_explicit_disabled_and_no_budget' in raw,
            'original host failure')
    corrected = read('cpu-v2/tests/result.json')
    c.require(corrected['passed'] and corrected['effective'] ==
        dict(wrappers=28, public_host=268, required_host=234, backend=9), 'CPU correction')
    for name in ('wrapper-zero', 'wrapper-zero_owner', 'host-off', 'host-on',
                 'inventory', 'generated'):
        c.require(read('cpu-v2/tests/' + name + '-exit.json')['returncode'] == 0,
                  'corrected check exit')
    for folder in ('cpu', 'cpu-v2'):
        build = read(folder + '/build-result.json')
        c.require(build['passed'] and build['zero_warnings'] and build['runtime_unchanged'],
                  'build evidence')
    c.require(read('driver-cpu/result.json')['passed'] and
        read('driver-cpu/result.json')['count'] == 5 and
        read('flags-correction-build/result.json')['passed'] and
        read('flags-correction-build/result.json')['tests'] == 3, 'driver CPU gates')
    windows = {}
    for folder, status in [('execution', 'COMMAND_FAILED'), ('execution-v2', 'COMPLETED')]:
        exit_value = read(folder + '/lease/exit.json')
        events = lines(folder + '/lease/events.jsonl')
        begin = [row for row in events if row['event'] == 'lease_acquired']
        end = [row for row in events if row['event'] == 'lease_released']
        observations = [row for row in events if row['event'] in ('preflight', 'monitor')]
        c.require(len(begin) == len(end) == 1 and exit_value['status'] == end[0]['status'] == status
            and all(not row['conflicts'] for row in observations), 'GPU guard evidence')
        seconds = end[0]['monotonic'] - begin[0]['monotonic']
        c.require(0 < seconds < 600, 'GPU bound')
        windows[folder] = dict(status=status, seconds=seconds,
                              observations=len(observations), records=len(events))
    original_gpu = read('execution/business/failed.json')
    c.require(original_gpu['counts'] == dict(native=1, starts=0, controls=0, generations=0)
        and b'FAIL: host flags preserved' in
        data['execution/business/native-mixed/stderr.log'], 'original native failure')
    out = 'execution-v2/business/'
    result = read(out + 'result.json')
    c.require(result['status'] == 'PASS_BOUNDED_CUDA_QUOTA' and result['cleanup_confirmed']
        and result['counts']['generations'] == 7 and result['counts']['starts'] == 3 and
        result['counts']['native'] == 6 and result['counts']['controls'] <= 120,
        'bounded business result')
    natives = []
    for mode in c.MODES:
        path = out + 'native-' + mode + '/'
        parsed = fix.parse_probe(mode, data[path + 'stdout.log'].decode())
        c.require(parsed == read(path + 'result.json') and
            read(path + 'waitable.json')['returncode'] == 0 and
            read(path + 'reaped.json')['returncode'] == 0, 'native result')
        natives.append(parsed)
    c.require(b'25 tests, 25 passed, 0 failed, 0 skipped' in
        data[out + 'lifecycle/stdout.log'], 'native HTTP lifecycle')
    rejected = read(out + 'startup-reject/quota-reports.json')[-1]
    c.require(read(out + 'startup-reject/waitable.json')['returncode'] == 1 and
        rejected['byte_rejections'] == 1 and
        rejected['charged_bytes'] == rejected['unknown_bytes'] == 0, 'startup refusal')
    models = {}
    for label, expected, sequences in [('auto', 5, 1), ('explicit', 2, 2)]:
        values = read(out + label + '-metrics.json')
        verify.quota_metrics(values)
        c.require(values['q4t_requests_total'] == values['q4t_requests_success_total'] == expected
            and all(values[name] == 0 for name in verify.client.EMPTY)
            and values['q4t_seq_slots_total'] == values['q4t_seq_slots_free'] == sequences,
            'resource accounting')
        ended = read(out + label + '/quota-reports.json')[-1]
        c.require(ended['event'] == 'server_destroyed' and ended['unknown_bytes'] == 0
            and not ended['poisoned'] and read(out + label + '/waitable.json')['returncode'] == 0,
            'clean stop')
        models[label] = dict(limit_bytes=values['q4t_cuda_quota_limit_bytes'],
            peak_requested_bytes=values['q4t_cuda_quota_peak_bytes'],
            idle_requested_bytes=values['q4t_cuda_quota_charged_bytes'],
            destroyed_live_requested_bytes=ended['live_bytes'],
            grammar_reaped=values['q4t_structured_workers_reaped_total'])
    timings = {}
    for label in ('ordinary-json', 'ordinary-sse', 'strict-json', 'project-read',
                  'project-answer', 'concurrent-0', 'concurrent-1'):
        request = read(out + label + '-request.json')
        receipt = read(out + label + '-http.json')
        c.require(receipt['status'] == 200 and receipt['eof'], 'HTTP receipt')
        parser = verify.client.read_sse if request['stream'] else verify.client.read_json
        parsed = parser(io.BytesIO(data[out + label + '-wire.bin']))
        c.require(parsed.id == request['request_id'] and parsed.model == request['model']
            and parsed.finish_reason == ('tool_calls' if label == 'project-read' else 'stop'),
            'wire terminal/identity')
        if label.startswith(('ordinary-', 'concurrent-')):
            c.require(parsed.message['content'].strip() == '710003', 'ordinary quality')
        if label == 'strict-json':
            c.require(json.loads(parsed.message['content']) == {'ok': True}, 'strict output')
        if label == 'project-answer':
            tool = json.loads(read(out + 'project-tool-result.json')['content'])
            verify.client.validate_answer(parsed.message['content'], {tool['source']['citation']}, 1)
            c.require('Qwen4ExpForConditionalGeneration' in parsed.message['content'],
                      'real project quality')
        timings[label] = receipt['seconds']
    custody = read(out + 'custody.json')
    c.require(len(custody['retained']) == 10 and
        all(row['state'] == 'Z' for row in custody['retained']), 'complete process custody')
    query = [row for row in lines('execution-v2/lease/nvml-queries.jsonl')
             if row['request_id'] == custody['acknowledgement']['request_id']]
    last_start = read(out + 'explicit/identity.json')['registered_monotonic']
    c.require(verify.custody.acknowledgement(
        [custody['acknowledgement']['monitor']], query, last_start) is not None,
        'guard custody temporal evidence')
    post = read('postflight/result.json')
    c.require(post['passed'] and len(post['owned']) == 11 and
        all(row['owned_exited'] for row in post['owned']) and
        post['old_day_unchanged'] and post['old_day_not_executed'], 'postflight')
    c.require(read('postflight/no-restart.json')['passed'] and
        read('postflight/no-restart.json')['after_seconds'] >= 10, 'no restart observation')
    final = read('execution-v2/finalized.json')
    c.require(final['status'] == 'DELIVERED', 'default installation not finalized')
    plan = read('package-v2/plan.json')
    c.require(post['default']['sha256'] == plan['candidate_sha256'], 'delivered binary')
    return dict(status='ACCEPT_BOUNDED_CUDA_QUOTA_INTEGRATION',
        cpu_effective=corrected['effective'], driver_tests=5, flags_oracle_tests=3,
        original_cpu_failures_preserved=True, original_native_failure_preserved=True,
        native_starts=7, accepted_native_starts=6,
        accepted_buffers=sum(row['verified_buffers'] for row in natives),
        accepted_bytes=sum(row['verified_bytes'] for row in natives),
        accepted_checkpoints=sum(row['checkpoints'] for row in natives),
        native_http_tests=25, model_start_attempts=3, successful_model_starts=2,
        successful_generations=7, real_project_tool_executions=1,
        controls=result['counts']['controls'], models=models, request_seconds=timings,
        gpu_windows=windows, default=post['default'], sidecar=post['sidecar'],
        protocol_sha256=plan['protocol_sha256'], runtime_sources=len(read(
            'package-v2/candidate/runtime-source.json')),
        operator_plan_prepared='operator-soak/day/plan.json' in data,
        physical_ram_hard_limit=False, performance_acceptance=False, day_executed=False)


def collect():
    data, local = {}, {}
    def add(path):
        if not path.is_file() or path.is_symlink():
            return
        with path.open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
        if elf:
            local[str(path)] = c.identity(path)
        else:
            data[str(path.relative_to(c.BASE))] = path.read_bytes()
    for folder in ('cpu', 'cpu-v2', 'driver-cpu', 'flags-correction-build',
                   'execution', 'execution-v2', 'postflight', 'report-audit',
                   'operator-soak', 'operator-package'):
        for path in (c.BASE / folder).rglob('*'):
            add(path)
    for folder in ('package', 'package-v2'):
        for path in (c.BASE / folder).rglob('*'):
            if 'cpu' not in path.relative_to(c.BASE / folder).parts:
                add(path)
    data['archiver/report.py'] = Path(__file__).read_bytes()
    add(c.BASE / 'operator-package-preparation.json')
    for name in (c.NEW_RUNTIME):
        data['runtime-source/' + name] = (ROOT / name).read_bytes()
    return data, local


def archive():
    data, local = collect()
    result = evaluate(data)
    DEST.mkdir(parents=True)
    files = {name: dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
             for name, raw in sorted(data.items())}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as target:
        for name, raw in sorted(data.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o600, 0
            target.addfile(info, io.BytesIO(raw))
    packed = gzip.compress(buffer.getvalue(), mtime=0)
    with (DEST / 'records.tar.gz').open('xb') as stream:
        stream.write(packed)
    sources = {str(path.relative_to(ROOT)): c.sha(path) for path in
        [Path(__file__), Path(c.__file__), Path(verify.__file__), Path(fix.__file__)]}
    c.save(DEST / 'manifest.json', dict(schema=1, files=files, sources=sources,
        local_identities=local, archive=dict(bytes=len(packed),
            sha256=hashlib.sha256(packed).hexdigest())))
    c.save(DEST / 'result.json', result)
    print(json.dumps(dict(records=len(data), local_identities=len(local),
        compressed_bytes=len(packed), result=result), ensure_ascii=False))


def verify_archive():
    manifest = c.read(DEST / 'manifest.json')
    for name, digest in manifest['sources'].items():
        c.require(c.sha(ROOT / name) == digest, 'archiver dependency identity')
    raw = (DEST / 'records.tar.gz').read_bytes()
    c.require(len(raw) == manifest['archive']['bytes'] and
        hashlib.sha256(raw).hexdigest() == manifest['archive']['sha256'], 'archive identity')
    data = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as source:
        for member in source:
            c.require(member.isfile() and member.name not in data and
                not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                'archive member')
            data[member.name] = source.extractfile(member).read()
    files = {name: dict(bytes=len(value), sha256=hashlib.sha256(value).hexdigest())
             for name, value in data.items()}
    c.require(files == manifest['files'] and evaluate(data) == c.read(DEST / 'result.json'),
              'archived evidence changed')
    for value in manifest['local_identities'].values():
        c.require(c.identity(value['path']) == value, 'local binary identity')
    print('ARCHIVE_PASS', len(data), 'records', len(manifest['local_identities']), 'local identities')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'archive', 'verify-archive'])
    args = parser.parse_args()
    if args.action == 'check':
        print(json.dumps(evaluate(collect()[0]), indent=2))
    elif args.action == 'archive':
        archive()
    else:
        verify_archive()
