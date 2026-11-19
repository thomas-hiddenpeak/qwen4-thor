"""Review completed voice exchanges after the old health-name assertion failed.

This reads immutable original records; it cannot issue HTTP or load a model.
Only this specific, fully completed primary voice checklist can be reused.
"""
import hashlib
import json
from pathlib import Path

from tools.serve_ops import contracts
from tools.serve_recovery import profile_voice
from tools.serve_service.package import read, require, save, sha

FAILURE = ('tools.serve_recovery.ledger.LedgerError: '
           'qualified voice backend failed or depends on core readiness\n')
FORMAT = 'q4t-profile-primary-voice-review-v1'


class RecordedVoice(profile_voice.Lifecycle):
    def __init__(self, output, plan):
        from types import SimpleNamespace
        stage = SimpleNamespace(out=output, profile_name='primary', plan=plan,
                                http=self.configuration)
        super().__init__(stage)
        self.seen = set()

    def http(self, label, path, body=None, *, method=None, client=None,
             content_type='application/json'):
        require(label not in self.seen, 'recorded voice exchange reused twice')
        self.seen.add(label)
        method = method or ('GET' if body is None else 'POST')
        raw = (body if type(body) is bytes else None if body is None else
               json.dumps(body, ensure_ascii=False).encode())
        prefix = self.stage.out/('primary-voice-'+label)
        expected = dict(method=method, path=path,
            client=client or profile_voice.encrypted.CLIENT,
            content_type=content_type, body_bytes=0 if raw is None else len(raw),
            body_sha256=None if raw is None else hashlib.sha256(raw).hexdigest())
        require(read(str(prefix)+'.request.json') == expected,
                'recorded voice request changed: '+label)
        if raw is not None:
            require(Path(str(prefix)+'.request.body').read_bytes() == raw,
                    'recorded voice body changed: '+label)
        if method == 'POST':
            self.requests += 1
        wire = Path(str(prefix)+'.wire').read_bytes()
        require(len(wire) <= profile_voice.MAX_RESPONSE_BYTES,
                'recorded voice response bound')
        return read(str(prefix)+'.http.json')['status'], wire

    def configuration(self, path, *, label):
        require(path == '/v1/config' and label == 'primary-voice-configuration',
                'unexpected recorded configuration request')
        prefix = self.stage.out/label
        return (read(str(prefix)+'.http.json')['status'],
                Path(str(prefix)+'.wire').read_bytes())

    def conditioning(self, consent, voice):
        value = read(self.stage.out/'primary-voice-conditioning.json')
        profile_voice.evidence.voice_reference(value, consent, voice)
        return value


def original_environment(out):
    result = read(out/'result.json')
    require(result['status'] == 'FAILED' and result['error'].endswith(FAILURE) and
        'evidence.voice_business(result, self.stage.plan' in result['error'] and
        result['counts']['starts'] == 1 and result['counts']['chats'] == 12 and
        result['counts']['websocket_sessions'] == result['counts']['ingress_starts'] == 0,
        'only completed primary voice health-name failure can be reviewed')
    lease = read(out/'lease/exit.json')
    require(lease['status'] == 'COMMAND_FAILED' and lease['child_returncode'] == 1 and
        lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'] is True,
        'original failure was not the isolated validation failure')
    queries = [json.loads(row) for row in (out/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries),
            'original voice environment not observed or failed')
    cleanup = read(out/'cleanup.json')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True,
        'original owned model did not cleanly stop')
    return dict(status='ACCEPT_REVIEWED_ENVIRONMENT', nvml_queries=len(queries),
        model_starts=1, requests=12, seconds=result['seconds'],
        lease_result_sha256=sha(out/'lease/exit.json'),
        original_command_status='COMMAND_FAILED', original_failure_preserved=True)


def prepare(source, plan):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(profile_voice.encrypted.ROOT/'.q4t-work'),
            'private original voice stage required')
    original = read(source/'plan.json')
    require(original.get('voice_icl') is True and plan.get('voice_icl') is True and
        original['binary_sha256'] == plan['binary_sha256'] == sha(plan['binary']) and
        original['identity'] == plan['identity'] and original['profiles'] == plan['profiles'] and
        original['base']['settings'] == plan['base']['settings'] and
        original['voice_clips'] == plan['voice_clips'],
        'reused voice runtime, capacity, backends or inputs differ')
    for key in ('protected', 'native_assets'):
        for path, digest in original[key].items():
            require(sha(path) == digest, 'original voice dependency changed: '+path)
    environment = original_environment(source)
    actor = RecordedVoice(source, original)
    value = actor.run()
    expected = {p.name.removeprefix('primary-voice-').removesuffix('.request.json')
                for p in source.glob('primary-voice-*.request.json')}
    require(actor.seen == expected and actor.requests == 12,
            'original voice checklist has omitted or extra exchanges')
    output = Path(plan['output'])
    save(output/'reviewed-primary-voice.json', value)
    # Keep original raw wires, reference codes, runtime declaration and failures.
    files = list(source.glob('primary-voice-*')) + [source/n for n in
        ('plan.json', 'result.json', 'cleanup.json', 'counts.json',
         'primary-config.json', 'primary-capabilities.json',
         'lease/exit.json', 'lease/nvml-queries.jsonl')]
    files.append(output/'reviewed-primary-voice.json')
    return dict(format=FORMAT, source=str(source), environment=environment,
        receipt=str(output/'reviewed-primary-voice.json'),
        configuration=contracts.configuration(read(source/'primary-config.json'), version=5),
        capabilities=read(source/'primary-capabilities.json'),
        files={str(p):sha(p) for p in files if p.is_file()},
        source_commit=original['source_commit'],
        scope='completed original primary voice only; subsequent process runs remaining requests')


def consume(stage):
    selected = stage.plan['retained_primary_voice']
    require(stage.profile_name == 'primary' and selected['format'] == FORMAT,
            'only original primary voice may be reused')
    for path, digest in selected['files'].items():
        require(sha(path) == digest, 'original voice evidence drift: '+path)
    require(read(stage.out/'primary-capabilities.json') == selected['capabilities'] and
        contracts.configuration(read(stage.out/'primary-config.json'), version=5) ==
            selected['configuration'], 'current runtime differs from original voice context')
    value = read(selected['receipt'])
    profile_voice.evidence.voice_business(value, stage.plan['identity'], icl=True)
    save(stage.out/'primary-voice-reuse.json', dict(
        source=selected['source'], actual_new_requests=0, reused_requests=12,
        original_failure_preserved=True, original_process_reused=False))
    return value


def combined_environment(out, selected, current):
    original = original_environment(Path(selected['source']))
    require(original == selected['environment'], 'original environment changed')
    value = dict(current=current, original=original, source=selected['source'],
        original_failure_preserved=True, separate_continuous_leases=True)
    save(out/'combined-environment.json', value)
    return dict(current, nvml_queries=current['nvml_queries']+original['nvml_queries'],
                lease_result_sha256=sha(out/'combined-environment.json'))
