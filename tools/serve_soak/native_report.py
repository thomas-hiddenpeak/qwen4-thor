"""Read-only native soak verification from original wires and custody records."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.examples import chat_client
from tools.serve_ops import contracts as ops_contracts
from tools.serve_soak import evidence
from tools.serve_soak import native_contract as contract
from tools.serve_soak import native_observations


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def seal(output):
    """Freeze completed evidence; it never certifies successful execution."""
    output = Path(output)
    files = [output/name for name in ('plan.json', 'run.json', 'lease/exit.json')]
    for directory in ('execution', 'capture'):
        files += [p for p in (output/directory).rglob('*') if p.is_file()]
    contract.require(len(files) < 100000 and all(not p.is_symlink() and
        p.resolve().is_relative_to(output.resolve()) for p in files),
        'evidence file count or ownership boundary differs')
    value = dict(version=1, files={str(p.relative_to(output)): sha(p)
                                  for p in sorted(files)})
    with (output/'seal.json').open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    return value


def verify_seal(output):
    output = Path(output)
    value = read(output/'seal.json')
    contract.require(value.get('version') == 1 and
        type(value.get('files')) is dict and 0 < len(value['files']) < 100000,
        'invalid evidence seal')
    for name, digest in value['files'].items():
        path = output/name
        contract.require(not Path(name).is_absolute() and '..' not in
            Path(name).parts and not path.is_symlink() and
            path.resolve().is_relative_to(output.resolve()) and sha(path) == digest,
            'sealed evidence changed: '+name)
    return value


def require_records(plan, sealed):
    required = {'plan.json', 'run.json', 'lease/exit.json',
        'capture/capture.json', 'capture/events.jsonl',
        'capture/nvml-queries.jsonl', 'execution/status.jsonl', 'execution/result.json',
        'execution/counts.json', 'execution/stopped-ledger.json',
        'execution/launcher-result.json', 'execution/initial-ready.json',
        'execution/soak.json', 'execution/self-check-1.json'}
    for number in range(1, plan['cycles']+1):
        prefix = 'execution/cycle-'+str(number).zfill(4)
        for label in contract.LABELS:
            required.update(prefix+'-'+label+suffix for suffix in (
                '-request.json', '-response.json', '-wire.bin'))
        if number in plan.get('audio_cycles', ()):
            from tools.serve_soak import native_audio
            required.update(prefix+'-'+name for name in native_audio.LABELS)
        required.update((prefix+'-tool-executed.json', prefix+'-idle.json',
                         prefix+'-idle-metrics.json'))
    if plan['profile'] == 'day':
        required.add('execution/first-hour.json')
    contract.require(required <= sealed['files'].keys(),
                     'required native soak records missing from seal')


def verify_retention(lease, logs):
    size = evidence.ROTATION_BYTES
    contract.require(lease.get('retention') == dict(bytes_per_file=size,
        generations=2, complete_history_retained=False),
        'guard did not enable the expected log rotation')
    contract.require(set(logs) == {'events.jsonl', 'nvml-queries.jsonl'},
                     'guard capture must contain both monitor logs')
    for name, item in logs.items():
        contract.require(item.get('complete_observed_chain') is True and
            type(item.get('bytes')) is int and item['bytes'] > 0 and
            type(item.get('rotations')) is int and
            item['rotations'] == (item['bytes']-1)//size,
            'guard capture has incomplete rotations: '+name)


def check(output):
    output = Path(output).resolve(strict=True)
    sealed = verify_seal(output)
    plan = read(output/'plan.json')
    config = contract.validate_plan(plan)
    require_records(plan, sealed)
    if plan['version'] == 2:
        from tools.serve_soak import native_audio
        module = 'tools/serve_soak/native_audio.py'
        contract.require(sha(ROOT/module) == plan['sources'][module], 'audio verifier source differs')
        package = Path(plan['package']['package'])
        manifest = package/'manifest.json'
        contract.require(sha(manifest) == plan['package']['manifest_sha256'] and
                         native_audio.binding(package, read(manifest)) == plan['audio_roundtrip'],
                         'audio reference differs from the issued package')

    contract.require(Path(plan['output']) == output, 'evidence plan path changed')
    for name in ('tools/serve_soak/native_contract.py',
                 'tools/serve_soak/native_report.py', 'tools/serve_soak/native_observations.py',
                 'tools/serve_soak/evidence.py', 'tools/examples/chat_client.py',
                 'tools/serve_ops/contracts.py'):
        contract.require(sha(ROOT/name) == plan['sources'][name],
                         'verification implementation differs from frozen plan')
    run = read(output/'run.json')
    result = read(output/'execution/result.json')
    contract.require(run['status'] == 'PASS' and run['exit_code'] == 0 and
        run['seconds'] <= config['wall_seconds'] and
        result['status'] == 'PASS_BOUNDED_RECOVERY_BUSINESS' and
        result['failure'] is None and result['cleanup_failure'] is None and
        result['package'] == plan['package'], 'native lifecycle did not pass')
    counts = result['budget']['counts']
    contract.require(counts['starts'] == 1 and counts['probes'] == 0 and
        counts['chats'] == contract.business_count(config, config['cycles'])+1 and
        counts['controls'] <= config['limits']['controls'], 'actual count differs')
    contract.require(read(output/'execution/counts.json') == result['budget'],
                     'final bounded counters differ from lifecycle receipt')
    capture = read(output/'capture/capture.json')
    contract.require(capture['passed'], 'continuous guard capture incomplete')
    lease = read(output/'lease/exit.json')
    verify_retention(lease, capture['logs'])
    for name, item in capture['logs'].items():
        path = output/'capture'/name
        contract.require(path.stat().st_size == item['bytes'] and
                         sha(path) == item['sha256'], 'guard stream changed')
    guard = evidence.guard_archive(output/'capture/events.jsonl', lease)
    core = read(output/'execution/stopped-ledger.json')
    launcher = read(output/'execution/launcher-result.json')
    contract.require(core['desired'] == 'STOPPED' and
        all(v is None for v in core['active'].values()) and
        launcher['state'] == 'STOPPED' and launcher['cleanup_confirmed'],
        'original native service did not stop')
    initial = read(output/'execution/initial-ready.json')
    guard['nvml'] = native_observations.verify(output, lease, initial)
    identifiers, rows = set(), []
    digest = plan['sources'][contract.DOCUMENT]
    for number in range(1, config['cycles']+1):
        prefix = 'cycle-'+str(number).zfill(4)
        completions, requests = {}, {}
        for label in contract.LABELS:
            stem = prefix+'-'+label
            request = read(output/'execution'/(stem+'-request.json'))
            response = read(output/'execution'/(stem+'-response.json'))
            wire_path = output/'execution'/(stem+'-wire.bin')
            contract.require(str(wire_path.relative_to(output)) in sealed['files'],
                             'completion wire is not sealed')
            contract.require(response['status'] == 200 and
                request['request_id'] not in identifiers, 'request failed or replayed')
            identifiers.add(request['request_id'])
            wire = io.BytesIO(wire_path.read_bytes())
            parsed = asdict((chat_client.read_sse if request['stream'] else
                             chat_client.read_json)(wire))
            contract.require(parsed == response['value'] and
                parsed['id'] == request['request_id'] and
                parsed['model'] == request['model'], 'wire or identity differs')
            contract.validate_completion(label, parsed, digest)
            completions[label], requests[label] = parsed, request
        tool = read(output/'execution'/(prefix+'-tool-executed.json'))
        calls = completions['tool']['message']['tool_calls']
        contract.require(tool['executions'] == 1 and len(calls) == 1 and
            calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) ==
            dict(path=contract.DOCUMENT) and
            tool['source'] == dict(path=contract.DOCUMENT, sha256=digest) and
            tool['reply']['tool_call_id'] == calls[0]['id'] and
            json.loads(tool['reply']['content']) == dict(path=contract.DOCUMENT,
                sha256=digest, answer=digest[:12]) and
            requests['answer']['messages'][-2:] ==
            [completions['tool']['message'], tool['reply']],
            'actual tool execution or continuation differs')
        if number in plan.get('audio_cycles', ()):
            from tools.serve_soak import native_audio
            native_audio.verify(plan['audio_roundtrip'], output/'execution'/prefix)
        idle = read(output/'execution'/(prefix+'-idle.json'))
        contract.require(idle['model_pid'] == initial['model_pid'] and
            idle['model_start_ticks'] == initial['model_start_ticks'],
            'quiescent sample belongs to another model')
        metrics = idle['metrics']
        observations = read(output/'execution'/(prefix+'-idle-metrics.json'))[
            'observations']
        contract.require(bool(observations), 'missing operations metric response')
        response = observations[-1]['response']
        parsed_metrics, _ = ops_contracts.parse_metrics(response['body'], version=5)
        contract.require(response['http_status'] == 200 and
            parsed_metrics == observations[-1]['values'] == metrics,
            'operations wire differs from idle snapshot')
        contract.require(metrics['q4t_requests_total'] ==
            metrics['q4t_requests_success_total'] == contract.business_count(config, number)+1 and
            metrics['q4t_seq_slots_free'] == metrics['q4t_seq_slots_total'] == 1 and
            metrics['q4t_structured_workers_started_total'] ==
            metrics['q4t_structured_workers_reaped_total'], 'request owners not drained')
        rows.append(idle['resource'])
    soak = read(output/'execution/soak.json')
    expected = contract.checkpoint(plan['profile'], config['cycles'],
                                    soak['seconds'], final=True, audio=plan['version'] == 2)
    expected['envelope'] = (contract.resource_envelope(rows)
                             if plan['profile'] == 'day' else None)
    contract.require(soak == expected, 'final soak result differs')
    hour_path = output/'execution/first-hour.json'
    if plan['profile'] == 'day':
        hour = read(hour_path)
        n = 3600//config['interval']
        expected_hour = contract.checkpoint('day', n, hour['seconds'], audio=plan['version'] == 2)
        expected_hour['envelope'] = contract.resource_envelope(rows[:n])
        contract.require(hour == expected_hour, 'first hour incomplete')
    else:
        contract.require(not hour_path.exists(), 'short run claimed first hour')
    return dict(status='PASS_NATIVE_'+plan['profile'].upper()+'_SOAK',
        package=plan['package'], profile=plan['profile'], cycles=config['cycles'],
        business_requests=len(identifiers)+2*len(plan.get('audio_cycles', ())), tool_executions=config['cycles'],
        starts=1, guard=guard, native_cleanup_confirmed=True,
        day_scale_stability_proved=plan['profile'] == 'day', new_model_actions=0,
        performance_acceptance=False, physical_ram_hard_boundary_proved=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.output), ensure_ascii=False, indent=2))
