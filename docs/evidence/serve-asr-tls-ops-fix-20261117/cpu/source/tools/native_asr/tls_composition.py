"""Verify retained TLS business and separate same-engine private health proof.

Both original drivers remain FAILED. This auditor never starts a model and
never pretends that the missing final TLS configuration was observed there.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import runpy
import socket
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DRIVER = runpy.run_path(str(ROOT/'tools/native_asr/service_acceptance.py'))
require = DRIVER['require']
WORK = DRIVER['WORK']


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cleanup(directory):
    from tools.serve_recovery import bundle, journal
    plan = read(directory/'plan.json')
    out = directory/'execution'
    result = read(out/'result.json')
    guard = read(directory/'lease/exit.json')
    require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1
        and guard['compute_backend'] == 'nvml' and guard['external_lease'] is False
        and guard['lease_inherited_by_child'] is True and guard['command'] == plan['command'],
        'strict failed-command environment differs')
    queries = [json.loads(x) for x in (directory/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(x['status'] == 'OK' and x['returncode'] == 0 for x in queries),
            'missing or failed NVML observation')
    events = [json.loads(x) for x in (directory/'lease/events.jsonl').read_text().splitlines()]
    acquired = [x for x in events if x.get('event') == 'lease_acquired']
    released = [x for x in events if x.get('event') == 'lease_released']
    require(len(acquired) == len(released) == 1 and acquired[0]['pid'] == released[0]['pid']
        and released[0]['status'] == 'COMMAND_FAILED'
        and 0 < released[0]['monotonic']-acquired[0]['monotonic'] < plan['limits']['seconds'],
        'strict lease did not finish inside bound')
    stopped = read(out/'stop.json'); state = read(out/'state/ledger.json')['state']
    require(result['cleanup_failure'] is None and stopped['cleanup_confirmed']
        and stopped['stop_durable'] and state['desired'] == 'STOPPED'
        and all(v is None for v in state['active'].values()), 'cleanup incomplete')
    identities = {(x['pid'], x['start_ticks']) for x in result['models'].values()}
    for line in (out/'status.jsonl').read_text().splitlines():
        row = json.loads(line)
        for role in ('guardian', 'controller', 'model', 'gateway'):
            if row.get(role+'_pid') is not None:
                identities.add((row[role+'_pid'], row[role+'_start_ticks']))
    for pid, ticks in identities:
        try:
            require(journal.process_start(pid) != ticks, 'owned process still alive')
        except (FileNotFoundError, ProcessLookupError):
            pass
    package = Path(plan['package']['package'])
    manifest = bundle.verify(package, plan['package']['manifest_sha256'])
    policy = read(package/'gateway-policy.json')
    for key in ('listen_port', 'upstream_port', 'upstream_operations_port'):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', policy[key]))
    with Path(manifest['lock_path']).open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return dict(nvml_queries=len(queries), owned_processes_gone=True,
                ports_free=True, lock_released=True, env_abort=False)


def review(directory):
    directory = Path(directory).resolve(strict=True)
    require(directory.is_relative_to(WORK), 'private continuation required')
    plan = read(directory/'plan.json'); out = directory/'execution'
    original = Path(plan['reused_tls']['directory'])
    require(plan['scenario'] == 'tls_remaining' and
        DRIVER['reuse_tls'](original) == plan['reused_tls'], 'original completed observations drifted')
    for name, digest in plan['sources'].items():
        if name != 'tools/native_asr/service_acceptance.py':
            require(sha(ROOT/name) == digest, 'frozen TLS source changed: '+name)
    for row in [*plan['protected'], *plan['tls_inputs'].values()]:
        require(sha(row['path']) == row['sha256'], 'protected/TLS artifact changed')
    result = read(out/'result.json'); counts = result['budget']['counts']
    require(result['status'] == 'FAILED' and counts['starts'] == 1 and counts['chats'] == 3
        and counts['probes'] == 0 and counts['controls'] <= plan['limits']['controls']
        and 'RuntimeError: unexpected HTTP status: final-config' in result['failure']
        and read(out/'final-config-http.json')['status'] == 404
        and read(out/'final-config.wire')['error']['code'] == 'gateway_route_not_found',
        'unexpected continuation failure')
    cancel = read(out/'cancel-observation.json')
    for label, values in cancel.items():
        actual = DRIVER['metric_values'](read(out/(label+'-cancel.json'))[-1]['response']['body'])
        require(actual == values, 'cancellation metric projection differs')
        require(values['q4t_seq_slots_free'] == values['q4t_seq_slots_total'] == 1 and
            all(values[k] == 0 for k in ('q4t_active_chats', 'q4t_structured_cleanup_pending',
                'q4t_structured_workers_active', 'q4t_structured_active', 'q4t_structured_preparing'))
            and values['q4t_structured_workers_started_total'] ==
                values['q4t_structured_workers_reaped_total'], 'cancellation did not drain')
    require(cancel['after']['q4t_requests_aborted_total'] ==
        cancel['before']['q4t_requests_aborted_total']+1, 'actual abort missing')
    wire = (out/'cancel-stream.wire').read_text()
    events = [json.loads(x[6:]) for x in wire.splitlines() if x.startswith('data: {')]
    require(events and any(e['type'] == 'transcript.text.delta' and e['delta'] for e in events)
        and not any(e['type'] == 'transcript.text.done' for e in events), 'not an actual partial stream')
    core = read(out/'core-after.wire')['choices'][0]
    require(core['finish_reason'] == 'stop' and core['message']['content'].strip() == '42'
        and len(result['models']) == 1, 'core Chat failed or model restarted')
    first = original/'execution'
    python = read(first/'python-zh-json.sdk.json')
    require(python['result']['text'] == read(first/'python-zh-json.wire')['text'], 'Python projection changed')
    DRIVER['direct']().usage(python['result'], sdk=True)
    events = [json.loads(x[6:]) for x in (first/'js-en-stream.wire').read_text().splitlines()
              if x.startswith('data: {')]
    require(read(first/'js-en-stream.sdk.json')['result']['events'] == events, 'JS projection changed')
    environments = {name: cleanup(path) for name, path in [('original', original), ('continuation', directory)]}
    records = {str(p.relative_to(directory)): sha(p) for p in directory.rglob('*')
        if p.is_file() and (p.parent in (directory, directory/'lease', out)
            or p == out/'state/ledger.json') and p.suffix in ('.json', '.jsonl', '.wire', '.log')}
    return dict(status='TLS_BUSINESS_VERIFIED_FINAL_PRIVATE_HEALTH_PENDING',
        original=plan['reused_tls'], continuation=dict(directory=str(directory), files=records),
        package=plan['package'], counts=dict(starts=2, chats=8, probes=0),
        seconds=plan['reused_tls']['seconds']+result['seconds'], environments=environments,
        actual_cancellation_and_subsequent_chat=True, private_config_exposure_rejected=True,
        final_tls_private_health_observed=False, original_continuous_drivers_passed=False,
        default_replaced=False, new_gpu_starts=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--continuation', type=Path, required=True)
    parser.add_argument('--health', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve existing composition')
    value = review(args.continuation)
    if args.health:
        stage = args.health.resolve(strict=True)
        plan = read(stage/'plan.json'); decision = read(stage/'decision.json')
        require(decision['actual_business']['postflight']['sources_unchanged'] and
            decision['actual_business']['postflight']['owned_processes_gone'] and
            decision['actual_business']['env_abort'] is False and
            decision['manifest_sha256'] == plan['package']['manifest_sha256'], 'health acceptance incomplete')
        tls_package = Path(value['package']['package']); health_package = Path(plan['package']['package'])
        require(sha(tls_package/'engine/q4t') == sha(health_package/'engine/q4t') and
            read(tls_package/'engine/manifest.json')['settings']['native_asr'] ==
                read(health_package/'engine/manifest.json')['settings']['native_asr'],
            'ASR engine/asset/budget identity differs')
        health = read(stage/'execution/final-config.wire')['native_asr_health']
        require(health['state'] == 'ready' and health['compute_failures'] == 0 and
            health['core_readiness_dependency'] is False, 'private ASR health incomplete')
        value.update(status='ACCEPT_NATIVE_ASR_TLS_COMPOSED', health=health,
            health_from_separate_same_engine_service=True,
            health_evidence={str(stage/n): sha(stage/n) for n in
                ('plan.json', 'decision.json', 'execution/final-config.wire')})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps({k:value[k] for k in ('status', 'counts', 'seconds', 'new_gpu_starts')}))


if __name__ == '__main__':
    main()
