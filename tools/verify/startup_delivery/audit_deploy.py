"""Independent, read-only final delivery audit; never deploy or invoke CUDA.

Rechecks sealed candidate stages and two deployment health stages, including
locally generated evalscope SQLite rows. Only the new output JSON is written.
The currently installed default must match the accepted product candidate.
"""
import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pickle
import re
import sqlite3
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = ROOT / '.q4t-work/startup-delivery-20261021'
OLD_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
HEALTH = ('deploy-mtp-v1', 'deploy-plain-v1')
RETAINED = 'boundary-v1'
RETAINED_SEAL = 'd93e6e249fd6f8f370b69c43d401c015e756b0c64ac2c06fe480ab16e076c0cb'
RETAINED_AUDIT = 'ba445c7ac2506f16c0a3559f6687a398466394d6e0bee0a945beb78db63cc085'
SPEC = importlib.util.spec_from_file_location('independent_delivery_audit', HERE / 'audit.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
require, read, sha, identity = audit.require, audit.read, audit.sha, audit.identity


def check_identity(record):
    actual = identity(record['path'])
    require(actual == record, 'input identity differs: ' + record['path'])
    return Path(actual['path'])


def sealed_package(path, seal_record):
    """Package manifests use manifest.json; no process or mutation functions."""
    path = Path(path).resolve(strict=True)
    manifest = check_identity(seal_record)
    require(manifest == path / 'manifest.json', 'package manifest path differs')
    rows = read(manifest)
    require(set(rows) == {str(p.relative_to(path)) for p in path.rglob('*')
                         if p.is_file() and p != manifest}, 'package file set differs')
    for relative, digest in rows.items():
        target = (path / relative).resolve(strict=True)
        require(target.is_relative_to(path) and sha(target) == digest,
                'package seal differs: ' + str(target))
    return rows


class BasicUnpickler(pickle.Unpickler):
    """Evalscope messages are basic Python containers, never import globals."""
    def find_class(self, module, name):
        raise ValueError('unexpected pickle global: ' + module + '.' + name)


def raw_http(stage, expected_requests):
    """Recompute the exact result from the sealed, local SQLite database."""
    prefix = 'run/normal/http/'
    results = stage.document(prefix + 'results.json')
    require(len(results) == expected_requests, 'raw HTTP request count differs')
    source = stage.file('source/tools/evalscope/response_identity.py')
    helper = audit.pure_functions(source, ['response_identity'], dict(re=re))
    receipts = []
    for index, reported in enumerate(results):
        case = prefix + ('quality/' if index == 0 else 'quality-reuse/')
        names = [name for name in stage.manifest if name.startswith(case) and
                 name.endswith('/benchmark_data.db')]
        require(len(names) == 1, 'expected exactly one sealed evalscope database')
        database = stage.file(names[0])
        # Immutable/read-only mode avoids SQLite journals or shared-memory writes.
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1',
                             uri=True) as connection:
            rows = connection.execute('select success,prompt_tokens,completion_tokens,'
                'response_messages,first_chunk_latency,latency,request,start_time,'
                'completed_time from result order by start_time').fetchall()
        require(len(rows) == 1, 'raw database request count differs')
        row = rows[0]
        messages = BasicUnpickler(io.BytesIO(base64.b64decode(row[3], validate=True))).load()
        require(isinstance(messages, list) and all(isinstance(m, dict) for m in messages),
                'unexpected raw response structure')
        choices = [choice for msg in messages for choice in msg.get('choices', [])]
        text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                       for c in choices)
        finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
        request = audit.decode(row[6])
        joined = helper.response_identity(messages)
        require(row[0] and row[1] == 1024 and row[2] == 7 and text == '710003' and
                finish == ['stop'] and request['max_tokens'] == 32 and
                request['stream'] is True and joined['response_id_valid'],
                'raw HTTP quality/usage/response identity differs')
        prompt_sha = hashlib.sha256(request['prompt'].encode()).hexdigest()
        expected = dict(success=row[0], actual_input=row[1], actual_output=row[2],
                        text=text, finish=finish, ttft=row[4], latency=row[5],
                        prompt_sha256=prompt_sha, request_stream=True,
                        requested_max_tokens=32, **joined)
        require(all(reported[key] == value for key, value in expected.items()),
                'raw database and reported HTTP result differ')
        client = stage.document(case + 'client-exit.json')
        require(client['returncode'] == 0 and audit.finite(row[7]) and
                audit.finite(row[8]) and client['before']['monotonic_seconds'] <=
                row[7] <= row[8] <= client['after']['monotonic_seconds'],
                'raw HTTP timing is outside client boundaries')
        require(reported['timing']['start_monotonic_seconds'] == row[7] and
                reported['timing']['end_monotonic_seconds'] == row[8],
                'reported monotonic timing differs from database')
        receipts.append(dict(database=identity(database), response_id=joined['response_id'],
                             input_tokens=1024, output_tokens=7, text=text,
                             ttft_seconds=row[4], latency_seconds=row[5]))
    return receipts


class StageView:
    """Map shared frozen tools and scope receipt names without changing files."""
    def __init__(self, stage, outer=None):
        self.stage, self.outer = stage, outer
        self.path = stage.path
        self.manifest = dict(stage.manifest)
        if outer:
            self.manifest.update({name: digest for name, digest in outer.manifest.items()
                                  if name.startswith('source/')})

    def file(self, name):
        return (self.outer.file(name) if self.outer and name.startswith('source/')
                else self.stage.file(name))

    def text(self, name):
        return self.file(name).read_text()

    def document(self, name):
        value = read(self.file(name))
        if self.outer and name == 'state.json':
            return dict(value, scope_final=value['scope_cleanup'])
        return value

    def lines(self, name):
        return [audit.decode(line) for line in self.text(name).splitlines() if line]


def deployment_configuration(name):
    require(name in HEALTH, 'unknown deployment health stage')
    return dict(real_default=True, max_seq=1, max_prefill=0, max_len=0,
                resident_slots=0, mtp=name == HEALTH[0], startup_plan='auto',
                l2_slots=None, mirror_k=None, load_threads=None, memory_max=None,
                cache='inherited', requests=1)


def frozen_health_helper(stage):
    namespace = dict(json=json, Path=Path, re=re, ROOT=ROOT, BASE=BASE,
        BINARY=ROOT / 'build/q4t', MODEL=audit.MODEL, STAGES=audit.STAGES,
        DEPLOY_STAGES=HEALTH, BASE_ENVIRONMENT={}, HOST_LIMIT=64 << 30)
    names = ['require', 'records', 'configuration', 'port_for', 'environment',
             'server_command', 'capacity_evidence', 'reservation_evidence']
    helper = audit.pure_functions(stage.file('source/tools/verify/startup_delivery/run.py'),
                                  names, namespace)
    require(helper.configuration(stage.path.name) ==
            deployment_configuration(stage.path.name), 'frozen health matrix differs')
    return helper


def process_gone(who):
    path = Path('/proc') / str(who['pid']) / 'stat'
    try:
        fields = path.read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return True
    return int(fields[19]) != who['start_ticks']


def scope_gone(unit):
    require(re.fullmatch(r'q4t-[A-Za-z0-9_.-]+\.scope', unit), 'unowned scope name')
    result = subprocess.run(['systemctl', 'show', unit, '--property=LoadState',
                             '--value'], capture_output=True, text=True, timeout=5)
    require(result.stdout.strip() == 'not-found', 'model scope remains: ' + unit)
    return dict(unit=unit, load_state=result.stdout.strip(), command_returncode=result.returncode)


def resources(stage, state, events, binary):
    exited = stage.document('resources/exit.json')
    require(state['resources'] == dict(returncode=0, result=exited) and
            exited['status'] == 'COMPLETED' and not exited['failures'] and
            not exited['truncated'], 'resources completion differs')
    systems, groups = stage.lines('resources/resources.jsonl'), stage.lines('resources/cgroup.jsonl')
    cache_names = sorted(n for n in stage.manifest if n.startswith('resources/cache/')
                         and n.endswith('.json'))
    require(len(systems) == exited['system_samples'] > 0 and
            len(groups) == exited['cgroup_samples'] > 0 and
            len(cache_names) == exited['cache_samples'] > 0 and
            all(not row['failures'] for row in systems + groups),
            'resource sample counts/failures differ')
    for name in cache_names:
        audit.cache_vector(stage.document(name))
    before = audit.one(events, 'server_before_start')['monotonic_seconds']
    after = audit.one(events, 'server_after_shutdown')['monotonic_seconds']
    require(exited['start']['monotonic'] <= before <= after <=
            exited['finished']['monotonic'], 'resources did not cover model lifetime')
    ready = stage.document('resources/ready.json')
    actual = binary.stat()
    require(ready['binary'] == str(binary) and ready['binary_dev'] == actual.st_dev
            and ready['binary_inode'] == actual.st_ino and ready['model_dir'] ==
            str(audit.MODEL), 'resource observer followed another executable')
    who = stage.document('resources-identity.json')
    require(who['pid'] == ready['pid'] and process_gone(who), 'resource observer remains')
    return dict(system_samples=len(systems), cgroup_samples=len(groups),
                cache_samples=len(cache_names), start=exited['start'],
                finished=exited['finished'], observer=who, observer_gone=True)


def audit_health(outer, name, build):
    sealed = audit.SealedStage(outer.path / name)
    stage = StageView(sealed, outer)
    state = sealed.document('state.json')
    require(state['status'] == 'LOCAL_HEALTH_COMPLETED' and not state['errors'] and
            state['model_limit'] == state['http_limit'] == 1 and
            state['collector_profile'] == 'resources_nvml_production_summary' and
            state['kernel_timeline'] == 'NOT_COLLECTED' and
            state['lease_output'] == str(outer.path / 'lease') and
            state['http_returncode'] == 0 and not state['helper_cleanup']['started'],
            'deployment health state differs')
    cfg = deployment_configuration(name)
    helper = frozen_health_helper(stage)
    installed_build = dict(build, candidate=dict(build['candidate'], path=str(ROOT / 'build/q4t')))
    result = audit.audit_http(stage, installed_build, cfg, helper)
    events = result.pop('events')
    result['scope'] = audit.audit_scope(stage, cfg)
    result['cache'] = audit.audit_cache(stage, cfg, events, None)
    result['resources'] = resources(stage, state, events, ROOT / 'build/q4t')
    result['raw_http'] = raw_http(stage, 1)
    who = stage.document('run/normal/http/isolation/identity.json')
    require(process_gone(who), 'model process birth identity remains')
    result['scope_current'] = scope_gone(result['scope']['unit'])
    require(state['started']['monotonic'] <=
            audit.one(events, 'server_before_start')['monotonic_seconds'] and
            state['finished']['monotonic'] >=
            audit.one(events, 'server_after_shutdown')['monotonic_seconds'],
            'health supervisor did not cover model lifetime')
    result.update(name=name, status='PASS', model_starts=1, http_requests=1,
                  started=state['started'], finished=state['finished'],
                  seal=identity(sealed.manifest_path), model_process_gone=True)
    return result


def verify_release(outer, build, context):
    plan = outer.document('installation-plan.json')
    binary, sidecar = ROOT / 'build/q4t', ROOT / 'build/q4t.release.json'
    package = Path(plan['package_manifest']['path']).parent
    require(package.is_relative_to(ROOT / '.q4t-work/releases') and
            context['package'] == str(package) and context['output'] == str(outer.path),
            'release package/output path differs')
    sealed_package(package, plan['package_manifest'])
    sealed_package(outer.path / 'previous', plan['previous_manifest'])
    require(plan['schema'] == 1 and plan['previous_sha256'] == OLD_SHA and
            plan['candidate_sha256'] == build['candidate_sha256'] != OLD_SHA and
            plan['previous'] == context['previous'] == build['default_pair'] and
            plan['destinations'] == [str(binary), str(sidecar)], 'installation identity differs')
    for key, name in (('binary', 'q4t'), ('sidecar', 'q4t.release.json'),
                      ('build_cache', 'CMakeCache.txt')):
        require(sha(outer.path / 'previous' / name) == build['default_pair'][key]['sha256'],
                'rollback backup differs: ' + key)
    old = read(outer.path / 'previous/q4t.release.json')
    require(old['binary_sha256'] == OLD_SHA and old['build_cache_sha256'] ==
            sha(outer.path / 'previous/CMakeCache.txt'), 'old rollback pair/cache differs')
    require(not binary.is_symlink() and not sidecar.is_symlink() and
            sha(binary) == sha(package / 'q4t') == build['candidate_sha256'] and
            sha(sidecar) == sha(package / 'q4t.release.json') ==
            plan['candidate_release']['sha256'], 'published pair differs')
    new = read(sidecar)
    fields = {'build_cache': ('CMakeCache.txt', 'build_cache_sha256'),
              'tested_source_manifest': ('source-inputs.json', 'tested_source_manifest_sha256'),
              'build_manifest': ('build.json', 'build_manifest_sha256'),
              'acceptance_audit': ('audit.json', 'acceptance_audit_sha256'),
              'source_commit_record': ('build-attempt.json', 'source_commit_record_sha256'),
              'acceptance_snapshot': ('acceptance-decision.md', 'acceptance_snapshot_sha256')}
    for field, (filename, digest_field) in fields.items():
        require(new[field] == str(package / filename) and
                new[digest_field] == sha(package / filename), 'published metadata link differs: ' + field)
    require(new['acceptance'] == 'ENGINEERING_ACCEPTED_RELIABILITY' and
            new['binary_sha256'] == build['candidate_sha256'] and
            sha(package / 'CMakeCache.txt') == build['real_build_cache']['sha256'] and
            sha(package / 'build.json') == context['build_manifest']['sha256'] and
            sha(package / 'audit.json') == context['audit']['sha256'] and
            sha(package / 'source-inputs.json') == build['source_manifest']['sha256'] and
            new['source_commit'] == read(package / 'build-attempt.json')['source_commit'] and
            new['previous_binary'] == str(outer.path / 'previous/q4t') and
            new['previous_release'] == str(outer.path / 'previous/q4t.release.json'),
            'published release provenance differs')
    return dict(binary=identity(binary), sidecar=identity(sidecar),
                real_build_cache=identity(package / 'CMakeCache.txt'),
                package_manifest=plan['package_manifest'],
                rollback_manifest=plan['previous_manifest'], previous_sha256=OLD_SHA,
                atomicity=plan['atomicity'])


def retained_evidence(build, accepted):
    result = audit.retained_failure(build)
    expected = accepted['retained_failures']
    require(len(expected) == 1 and result['name'] == RETAINED and
            result['runtime_accepted'] is False and result['model_starts'] ==
            result['http_requests'] == 0 and result['preparations'] == 1 and
            result['sealed_manifest']['sha256'] == RETAINED_SEAL and
            result['audit']['sha256'] == RETAINED_AUDIT and
            result['candidate_sha256'] == build['candidate_sha256'] and
            all(expected[0][key] == result[key] for key in (
                'name', 'runtime_accepted', 'model_starts', 'http_requests',
                'preparations', 'sealed_manifest', 'audit', 'candidate_sha256')),
            'retained preparation failure was omitted or relabelled')
    require(accepted['budget']['model_starts'] == 6 and
            accepted['budget']['http_requests'] == 9 and
            accepted['budget']['preparations'] == accepted['budget']['preparation_limit'] == 7,
            'revised candidate budget differs')
    return result


def run(deployment):
    outer = audit.SealedStage(deployment)
    context = outer.document('context.json')
    manifest_path = check_identity(context['build_manifest'])
    accepted_path = check_identity(context['audit'])
    build, accepted = read(manifest_path), read(accepted_path)
    spec = importlib.util.spec_from_file_location('final_delivery_build', HERE / 'build.py')
    build_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_tools)
    build_tools.verify(manifest_path, check_workspace=True, check_default=False)
    require(accepted['status'] == 'PASS_BOUNDED_RELIABILITY' and
            accepted['runtime_accepted'] is True and not accepted['errors'] and
            set(accepted['stages']) == set(audit.STAGES) and
            accepted['candidate_sha256'] == build['candidate_sha256'],
            'six-stage candidate acceptance missing or mismatched')
    candidate_reports = [audit.audit_stage(name, build) for name in audit.STAGES]
    require(all(r['runtime_accepted'] for r in candidate_reports),
            'candidate stage re-audit failed: ' + str(candidate_reports))
    candidate_raw = {name: raw_http(StageView(audit.SealedStage(BASE / name)),
                     audit.configuration(name)['requests']) for name in audit.STAGES}
    retained = retained_evidence(build, accepted)
    actual_attempts = {p.parents[3].name for p in
                       BASE.glob('*/run/normal/http/events.jsonl')}
    require(actual_attempts == set(audit.STAGES) | {RETAINED},
            'unexpected or omitted candidate attempt exists')
    require(accepted['build_manifest'] == context['build_manifest'] and
            accepted['source_manifest'] == build['source_manifest'] and
            accepted['default'] == build['default_pair'] and
            context['candidate'] == build['candidate'] and
            context['total_model_limit'] == 8 and context['total_http_limit'] == 11 and
            context['deployment_model_limit'] == context['deployment_http_limit'] == 2,
            'deployment/candidate/source/budget binding differs')
    for name, result in zip(audit.STAGES, candidate_reports):
        require(accepted['stages'][name]['sealed_manifest_sha256'] ==
                result['sealed_manifest_sha256'], 'accepted stage seal differs')
    for relative, digest in outer.document('sources.json').items():
        require(outer.manifest['source/' + relative] == digest and
                sha(ROOT / relative) == digest, 'deployed verifier/tool identity differs')
    state = outer.document('deployment.json')
    require(state['status'] == 'DEPLOYED_HEALTH_CHECKED' and state['loads'] ==
            state['requests'] == 2 and not state.get('guard_cleanup_error') and
            not state.get('forced_guard_kill') and not state.get('rollback') and
            state['persistent_service'] is False and state['performance_acceptance'] is False
            and not state['post_guard_cleanup']['errors'], 'deployment not completed cleanly')
    lease = outer.document('lease/exit.json')
    require(state['guard_exit'] == lease and lease['status'] == 'COMPLETED' and
            lease['child_returncode'] == 0 and lease['compute_backend'] == 'nvml' and
            lease['external_lease'] is True and lease['poll_interval_seconds'] == 1 and
            'cleanup_error' not in lease, 'outer deployment guard failed')
    require([n for n in outer.manifest if n.endswith('lease/exit.json')] ==
            ['lease/exit.json'], 'deployment must have one outer GPU lease')
    common, _ = audit.reused_helpers(outer)
    nvml = common.audit_nvml(outer, must_succeed=True)
    lease_events = outer.lines('lease/events.jsonl')
    lease_begin = audit.one(lease_events, 'lease_acquired')['monotonic']
    lease_end = audit.one(lease_events, 'lease_guard_finished')['monotonic']
    require(all(not row['conflicts'] for row in lease_events
                if row['event'] in ('preflight', 'monitor')) and
            audit.one(lease_events, 'lease_guard_finished')['status'] == 'COMPLETED',
            'guard observed conflicts or failed cleanup')
    health = [audit_health(outer, name, build) for name in HEALTH]
    require(lease_begin <= health[0]['started']['monotonic'] <=
            health[0]['finished']['monotonic'] <= health[1]['started']['monotonic'] <=
            health[1]['finished']['monotonic'] <= lease_end,
            'one lease did not cover both serial deployment health stages')
    installed_at = outer.document('installation-started.json')['monotonic']
    require(lease_begin <= installed_at <= health[0]['started']['monotonic'],
            'installation did not occur under the same deployment lease')
    worker = outer.document('worker-result.json')
    require(worker['status'] == 'HEALTH_COMPLETED' and worker['loads'] == worker['requests'] == 2
            and [r['stage'] for r in worker['health']] == list(HEALTH)
            and state['health'] == worker['health'],
            'deployment worker budget/result differs')
    for row, report in zip(worker['health'], health):
        require(row['sealed_files'] == report['seal'], 'health seal receipt differs')
    attempt = read(BASE / 'deployment-attempt.json')
    require(attempt['output'] == str(outer.path) and attempt['candidate'] == build['candidate']
            and attempt['model_limit'] == attempt['http_limit'] == 2 and attempt['no_retry'],
            'deployment attempt budget differs')
    starts = sum(r['model_starts'] for r in candidate_reports + health + [retained])
    requests = sum(r['http_requests'] for r in candidate_reports + health + [retained])
    require(starts == 8 and requests == 11 and
            accepted['budget']['model_starts'] == 6 and
            accepted['budget']['http_requests'] == 9, 'total delivery budget differs')
    release = verify_release(outer, build, context)
    lock = Path(lease['lock_path'])
    require(not os.path.lexists(lock.with_name(lock.name + '.recovery-required')) and
            read(lock).get('state') == 'released' and
            read(lock).get('output') == str(outer.path), 'deployment lease/recovery remains')
    return dict(schema=1, status='PASS_DEPLOYED_RELIABILITY', runtime_accepted=True,
                deployment_accepted=True, deployment=identity(outer.manifest_path),
                candidate_manifest=context['build_manifest'],
                candidate_acceptance=context['audit'], candidate_stage_reaudit=candidate_reports,
                retained_failures=[retained],
                candidate_raw_http=candidate_raw, health=health, nvml=nvml, release=release,
                budget=dict(model_starts=starts, http_requests=requests,
                            model_start_limit=8, http_limit=11,
                            candidate_cache_gate_attempts=7, cache_gate_attempt_limit=7),
                current_observation=dict(owned_models_gone=True, owned_collectors_gone=True,
                                         owned_scopes_removed=True, recovery_marker_absent=True),
                gpu_execution=False, cache_operations=False, performance_acceptance=False,
                limitations=['The recorded scoped process identities are gone at audit time; '
                    'no claim about future or unrelated processes.',
                    'One-second NVML polling cannot exclude shorter outside interference.',
                    'The two-slot candidate boundary used serial requests.',
                    'Only planned startup storage is covered by reservation finalization.',
                    'Deployment uses measured inherited cache; it is not a full-hot guarantee.',
                    'No driver causal finding, kernel timeline or performance claim.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, default=BASE / 'deploy-v1')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(BASE.resolve()) and not output.exists(),
            'audit output must be a new file under delivery BASE')
    require(not output.is_relative_to(BASE / 'candidate-v1'),
            'cannot write into the frozen candidate build')
    require(not any((parent / 'sealed-files.json').exists() or
                    (parent / 'manifest.json').exists()
                    for parent in output.parents if parent.is_relative_to(BASE.resolve())),
            'cannot write into sealed evidence')
    report = dict(schema=1, status='FAILED', runtime_accepted=False,
                  deployment_accepted=False, errors=[], gpu_execution=False,
                  cache_operations=False, performance_acceptance=False)
    try:
        report = run(args.deployment.resolve(strict=True))
        report['errors'] = []
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    report['audit_sources'] = {str(p): identity(p)
        for p in (Path(__file__).resolve(), HERE / 'audit.py', HERE / 'build.py')}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({k: report[k] for k in ('status', 'deployment_accepted', 'errors')}))
    return 0 if report['deployment_accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
