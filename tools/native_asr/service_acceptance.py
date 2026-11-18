"""Native ASR managed TLS, scoped uploads and exact retained/version recovery.

Reuses the owned-tree lifecycle, strict budgets and cleanup verifier. Actual
transcription wire is required; CPU fixtures never qualify a service payload.
"""
import base64
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import re
import socket
import ssl
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = runpy.run_path(str(ROOT/'tools/serve_responses/web_service_acceptance.py'))
GLOBALS = BASE['main'].__globals__
VERSION, MANAGED, LEGACY = BASE['VERSION'], BASE['MANAGED'], BASE['LEGACY']
require, save = BASE['require'], BASE['save']
LIMITS = dict(tool_retained=dict(starts=1, chats=3, probes=0, controls=1500, seconds=300),
              tool_version=dict(starts=2, chats=6, probes=1, controls=2500, seconds=600),
              tls=dict(starts=1, chats=7, probes=0, controls=2000, seconds=600),
              tls_remaining=dict(starts=1, chats=3, probes=0, controls=2000, seconds=600))
WORK = ROOT/'.q4t-work/serve-audio-native-20261117'


def limits_for(scenario, chat_audio=False, native_tts=False, retained_tts=False):
    require(all(type(v) is bool for v in (chat_audio, native_tts, retained_tts)),
            'invalid managed audio budget flags')
    limits = dict(LIMITS[scenario])
    if retained_tts:
        require(scenario == 'tls_remaining' and native_tts,
                'retained TTS requires its verified exact TLS predecessor')
        return limits
    if chat_audio:
        require(scenario != 'tls_remaining', 'new Chat audio requires its complete TLS stage')
        limits['chats'] += 2 if scenario in ('tool_version', 'tls') else 1
    if native_tts:
        require(scenario != 'tls_remaining', 'new TTS requires its complete TLS stage')
        limits['chats'] += 4 if scenario in ('tool_version', 'tls') else 2
    return limits


def configure_plan_limits(plan):
    expected = limits_for(plan['scenario'], plan.get('chat_audio', False),
                          plan.get('native_tts') is not None,
                          plan.get('reused_tls', {}).get('tts_complete', False))
    require(plan['limits'] == expected, 'managed audio frozen budget differs')
    # Reused actors/verifiers share this process-local namespace, never a live
    # service policy. Only the fixed feature-specific increment is accepted.
    GLOBALS['LIMITS'] = dict(LIMITS, **{plan['scenario']: expected})


def direct():
    # Import only after production namespaces have been pinned for an actual
    # managed run. The reference driver is never part of the production pack.
    from tools.native_asr import transcription_acceptance
    return transcription_acceptance


def requests():
    return direct().requests()


def prepare(args):
    bundle, _, _, _, _, engine = LEGACY['load_modules']()
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed source before freezing')
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    pack = args.package.resolve(strict=True)
    digest = engine.sha(pack/'manifest.json'); manifest = bundle.verify(pack, digest)
    policy = engine.read(pack/'gateway-policy.json')
    require(manifest['ingress_mode'] == 'native' and
        bool(manifest['tls']) == args.scenario.startswith('tls') and
        policy['listen_host'] == '127.0.0.1' and
        manifest['recovery_profiles']['identity'].get('native_asr'),
        'exact native ASR qualified payload/TLS mode required')
    from tools.serve_recovery import profile_contract, profile_evidence
    qualified = engine.read(pack/'profile-primary.json')
    chat_audio = profile_evidence.chat_audio_enabled(
        manifest['recovery_profiles']['identity'], qualified['capabilities'])
    native_tts = manifest['recovery_profiles']['identity'].get('native_tts')
    profile_contract.tts_capability_binding(native_tts, qualified['capabilities'])
    limits = limits_for(args.scenario, chat_audio, native_tts is not None,
        args.scenario == 'tls_remaining' and native_tts is not None and
        args.retained is not None)
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_recovery',
        'tools/serve_host', 'tools/serve_service', 'tools/serve_ops', 'tools/serve_gateway',
        'tools/evalscope', 'tools/examples', 'tools/release', 'tools/serve_responses',
        'tools/native_asr', 'tools/native_tts', 'src/service', 'include/q4t/service'], cwd=ROOT, text=True).splitlines()
    plan = dict(version=1, scenario=args.scenario, limits=limits,
        package=dict(package=str(pack), manifest_sha256=digest), output=str(out),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n), sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
             'build/service.release.json', 'build/service-native-state/ledger.json')],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        command=[sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                 'run', '--plan', str(out/'plan.json')], requests=requests(),
        default_replacement=False, automatic_request_replay=False,
        planned=dict(starts=limits['starts'], self_checks=limits['starts'],
            counted_post_requests=limits['chats']), scope=__doc__)
    if chat_audio: plan['chat_audio'] = True
    if native_tts is not None:
        plan['native_tts'] = native_tts
        backend = engine.read(pack/'engine/client-policy.json')
        alpha = next(c for c in backend['clients'] if c['id'] == 'alpha')
        maximum = native_tts['limits']['max_frames']*16+1 + \
            engine.read(pack/'engine/manifest.json')['settings']['profile']['max_tokens']
        require(alpha['max_output_tokens'] >= maximum,
                'managed client budget would truncate full speech output')
    reference = engine.read(WORK/'service-http-05/plan.json')
    plan['clips'] = reference['clips']
    plan['sdk'] = reference['sdk']
    plan['python_sdk'] = direct().python_identity()
    for row in plan['clips'].values():
        require(engine.sha(row['path']) == row['sha256'], 'real audio changed')
        plan['sources'][row['path']] = row['sha256']
    plan['sources'].update(plan['python_sdk']['files'])
    plan['sources'][plan['python_sdk']['python']] = plan['python_sdk']['executable_sha256']
    plan['sources'].update({str(Path(plan['sdk']['sdk_directory'])/name):digest
                            for name, digest in plan['sdk']['files'].items()})
    plan['sources'][plan['sdk']['node']['path']] = plan['sdk']['node']['sha256']
    from tools.native_asr import auxiliary_service
    from tools.serve_recovery import profile_evidence
    fixed = manifest['recovery_profiles']['identity']
    if profile_evidence.audio_auxiliaries(fixed):
        plan['audio_auxiliary'] = fixed
        source_path = Path(plan['clips']['zh']['path'])
        if profile_evidence.has_separator(fixed):
            source_path = getattr(args, 'separator_audio', None)
            require(source_path is not None, 'managed separator needs fixed mixed audio')
            source_path = source_path.resolve(strict=True)
            require(engine.sha(source_path) == profile_evidence.SEPARATOR_FILE_SHA,
                    'qualified mixed-speaker source differs')
        elif profile_evidence.has_speaker(fixed):
            source_path = ROOT/'.q4t-work/serve-diarization-20261118/http-01/returns.wav'
            require(engine.sha(source_path) == profile_evidence.SPEAKER_FILE_SHA,
                    'accepted returning-speaker source differs')
        plan['sources'][str(source_path)] = engine.sha(source_path)
        source = source_path.read_bytes()
        audio = out/'auxiliary.wav'
        audio.write_bytes(auxiliary_service.source(source, fixed))
        plan['auxiliary_clip'] = dict(path=str(audio), sha256=engine.sha(audio))
        plan['sources'][str(audio)] = engine.sha(audio)
        plan['requests']['zh'] = auxiliary_service.request(fixed)
        plan['requests']['en'] = auxiliary_service.request(fixed, stream=True)
    require(all(engine.sha(ROOT/name) == digest for name, digest in plan['sources'].items()),
            'frozen validation source changed')
    if args.scenario.startswith('tls'):
        # This bounded fixture uses its private self-signed certificate as CA.
        # Hostname and certificate verification stay enabled in both SDKs.
        cert = Path(manifest['tls_configuration']['certificate']).resolve(strict=True)
        fixture = out.parent
        require(cert.is_relative_to(fixture), 'dedicated private TLS fixture required')
        plan['tls_inputs'] = dict(ca=dict(path=str(cert), sha256=engine.sha(cert)))
        for key in ('certificate', 'key'):
            path = Path(manifest['tls_configuration'][key]).resolve(strict=True)
            require(path.is_relative_to(fixture), 'private TLS material required')
            plan['tls_inputs'][key] = dict(path=str(path), sha256=engine.sha(path))
        backend = engine.read(pack/'engine/client-policy.json')
        limits = {c['id']:c for c in backend['clients']}
        require(backend['version'] == 4 and
            limits['alpha']['max_file_body_bytes'] >= 3*1024**2 and
            limits['beta']['max_file_body_bytes'] == 65536,
            'distinct file upload limits required')
    if args.scenario == 'tls_remaining':
        require(args.retained is not None, 'original failed TLS evidence required')
        reused = reuse_tls(args.retained)
        require(reused['package'] == plan['package'], 'continuation must use exact original pack')
        plan['reused_tls'] = reused
        plan['prior_evidence'] = [dict(path=str(Path(reused['directory'])/name), sha256=digest)
                                  for name, digest in reused['files'].items()]
    if args.scenario == 'tool_version':
        plan['observe_cold_profiles'] = True
        from tools.serve_recovery import version_contract
        require(args.retained and args.release and args.decision,
                'finalized exact retained acceptance required')
        retained = args.retained.resolve(strict=True)
        acceptance = {name:dict(path=str(path.resolve(strict=True)), sha256=engine.sha(path))
            for name, path in [('release', args.release), ('decision', args.decision)]}
        plan['registration'] = version_contract.prepare(plan['package'], plan['package'],
            dict(package=str(retained), manifest_sha256=engine.sha(retained/'manifest.json')),
            str(uuid.uuid4()), fallback_acceptance=acceptance)
        helper = out/'counted-cuda-probe'
        helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
            str(Path(__file__).resolve()), 'probe', '--plan', str(out/'plan.json')])+
            '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        helper.chmod(0o500)
        plan['probe_wrapper'] = dict(path=str(helper), sha256=engine.sha(helper))
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=plan['limits'])))


def stream_value(raw):
    value = direct()
    events = [json.loads(line[6:]) for line in raw.decode().splitlines()
              if line.startswith('data: {')]
    if events and 'segments' in events[-1]:
        # Exact diarized event projection. The caller separately verifies real
        # source voices, global times, usage, budgets and backend health.
        require(events[-1]['type'] == 'transcript.text.done' and b'[DONE]' not in raw,
                'wrong diarized stream terminal')
        final = events[-1];deltas, segments, ids = [], [], []
        for event in events[:-1]:
            if event['type'] == 'transcript.text.delta':
                deltas.append(event['delta']);ids.append(event['segment_id'])
            else:
                require(event['type'] == 'transcript.text.segment', 'unexpected diarized event')
                segments.append(event)
        require(''.join(deltas) == final['text'] and ids == [v['id'] for v in segments] and
            len(segments) == len(final['segments']) and all(
            all(a[k] == b[k] for k in ('id','type','text','start','end','speaker'))
            for a,b in zip(segments, final['segments'])), 'diarized stream segment linkage differs')
        return final
    require(events and events[-1]['type'] == 'transcript.text.done' and
        all(e['type'] == 'transcript.text.delta' for e in events[:-1]) and
        b'[DONE]' not in raw, 'wrong transcription event terminal')
    final = events[-1]
    require(''.join(e['delta'] for e in events[:-1]) == final['text'] and
        [p for e in events[:-1] for p in e.get('logprobs', [])] == final['logprobs'],
        'delta/final text or probabilities differ')
    value.usage(final)
    return final


def metric_values(raw):
    """Keep the public sanitized parser; add one exact native cleanup gauge."""
    from tools.serve_ops import contracts
    values, _ = contracts.parse_metrics(raw, version=5)
    rows = [line for line in raw.splitlines()
            if line.startswith('q4t_structured_cleanup_pending')]
    require(len(rows) == 1 and re.fullmatch(r'q4t_structured_cleanup_pending [0-9]+', rows[0]),
            'missing or invalid native cleanup gauge')
    value = int(rows[0].split()[1])
    require(value < 2**64, 'native cleanup gauge overflow')
    return dict(values, q4t_structured_cleanup_pending=value)


def reuse_tts_tls(directory, plan, outcome):
    """Keep completed real TLS work, never relabel the original cancellation failure."""
    read = lambda n: json.loads((directory/n).read_text())
    require(plan['scenario'] == 'tls' and outcome['status'] == 'FAILED' and
        outcome['cleanup_failure'] is None and outcome['budget']['counts']['starts'] == 1 and
        outcome['budget']['counts']['chats'] == limits_for('tls', plan.get('chat_audio', False), True)['chats'] and
        outcome['budget']['counts']['probes'] == 0 and
        'native disconnect failed to cancel actual transcription' in outcome['failure'],
        'unexpected completed TTS TLS predecessor')
    reviewed = {'tools/native_asr/service_acceptance.py', 'tools/native_tts/managed_service.py'}
    changed = {}
    for name, expected in plan['sources'].items():
        actual = hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        if actual != expected:
            require(name in reviewed, 'completed TLS source changed: '+name)
            changed[name] = dict(before=expected, after=actual)
    from tools.serve_recovery import bundle
    bundle.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    for row in [*plan['protected'], *plan['tls_inputs'].values()]:
        require(hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256'],
                'completed TTS TLS protected identity changed')
    guard = read('lease/exit.json')
    require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1 and
        guard['compute_backend'] == 'nvml' and guard['lease_inherited_by_child'] is True and
        guard['external_lease'] is False and guard['command'] == plan['command'],
        'environment failure cannot qualify completed TTS TLS observations')
    queries = [json.loads(line) for line in (directory/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries),
            'completed TTS TLS monitoring gap')
    state = read('execution/state/ledger.json')['state']; stopped = read('execution/stop.json')
    require(stopped['cleanup_confirmed'] and stopped['stop_durable'] and
        state['desired'] == 'STOPPED' and all(v is None for v in state['active'].values()),
        'completed TTS TLS cleanup is incomplete')
    observed = check_business(directory/'execution', verify_cancel=False)
    require(observed['cancellation'] == 'pending', 'original cancellation must remain unaccepted')
    records = [p for p in directory.rglob('*') if p.is_file() and
        (p.parent in (directory, directory/'lease', directory/'execution') or
         p == directory/'execution/state/ledger.json') and
        p.suffix in ('.json', '.jsonl', '.wire', '.log')]
    return dict(status='COMPLETED_TTS_TLS_REUSED_ORIGINAL_CANCELLATION_FAILED',
        directory=str(directory), package=plan['package'], tts_complete=True,
        files={str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest() for p in records},
        reviewed_driver_changes=changed, counts=outcome['budget']['counts'],
        seconds=outcome['seconds'], nvml_queries=len(queries),
        native_gateway_recovered_without_model_restart=True,
        original_cancellation_accepted=False, completed_business=observed)


def reuse_tls(directory):
    """Recheck completed observations without changing the original FAILED."""
    directory = Path(directory).resolve(strict=True)
    require(directory.is_relative_to(ROOT/'.q4t-work'), 'TLS predecessor outside private workspace')
    read = lambda n: json.loads((directory/n).read_text())
    plan, outcome = read('plan.json'), read('execution/result.json')
    if plan.get('native_tts') is not None:
        return reuse_tts_tls(directory, plan, outcome)
    require(directory.is_relative_to(WORK), 'legacy TLS predecessor outside original workspace')
    require(plan['scenario'] == 'tls' and outcome['status'] == 'FAILED' and
        outcome['cleanup_failure'] is None and
        "KeyError: 'q4t_structured_cleanup_pending'" in outcome['failure'] and
        outcome['budget']['counts']['starts'] == 1 and
        outcome['budget']['counts']['chats'] == 5 and
        outcome['budget']['counts']['probes'] == 0, 'unexpected TLS predecessor')
    for name, digest in plan['sources'].items():
        if name != 'tools/native_asr/service_acceptance.py':
            require(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest,
                    'completed TLS source changed: '+name)
    from tools.serve_recovery import bundle
    bundle.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    for row in [*plan['protected'], *plan['tls_inputs'].values()]:
        require(hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256'],
                'TLS predecessor protected artifact changed')
    guard = read('lease/exit.json')
    require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1 and
        guard['compute_backend'] == 'nvml' and guard['lease_inherited_by_child'] is True and
        guard['external_lease'] is False and guard['command'] == plan['command'],
        'environment abort cannot qualify completed TLS observations')
    queries = [json.loads(line) for line in (directory/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries),
            'TLS predecessor monitoring gap')
    state = read('execution/state/ledger.json')['state'];stopped = read('execution/stop.json')
    require(stopped['cleanup_confirmed'] and stopped['stop_durable'] and
        state['desired'] == 'STOPPED' and all(v is None for v in state['active'].values()),
        'TLS predecessor cleanup incomplete')
    out = directory/'execution'
    for label in ('python-zh-json', 'chunked-zh'):
        answer = read('execution/'+label+'.wire')
        direct().usage(answer);direct().quality(answer['text'], 'zh')
    direct().quality(stream_value((out/'js-en-stream.wire').read_bytes())['text'], 'en')
    for label, status in [('beta-file-limit', 413), ('unauthorized', 401), ('chunked-zh', 200)]:
        require(read('execution/'+label+'-http.json')['status'] == status, 'TLS scope/transport result differs')
    require(read('execution/chunked-zh-http.json')['chunked'] is True, 'chunked evidence missing')
    for label, version in [('python-zh-json', '2.8.1'), ('js-en-stream', '7.47.0')]:
        sdk = read('execution/'+label+'.sdk.json')
        require(sdk['ok'] and sdk['version'] == version and len(sdk['exchanges']) == 1,
                'TLS SDK completed observation differs')
    before = read('execution/ready-1.json')
    statuses = [json.loads(line) for line in (out/'status.jsonl').read_text().splitlines()]
    after = [s for s in statuses if s['business_ready'] and
             s['gateway_generation'] == before['gateway_generation']+1]
    require(after and all(after[-1][k] == before[k] for k in
        ('model_pid', 'model_start_ticks', 'controller_pid', 'controller_start_ticks')),
        'gateway recovery did not preserve healthy core')
    records = [p for p in directory.rglob('*') if p.is_file() and
        (p.parent == directory or p.parent == directory/'lease' or p.parent == out or
         p == out/'state/ledger.json') and p.suffix in ('.json', '.jsonl', '.wire', '.log')]
    # Private credentials and runtime configs never leave the original folder.
    files = {str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in records}
    return dict(status='COMPLETED_TLS_OBSERVATIONS_REUSED_ORIGINAL_FAILED',
        directory=str(directory), package=plan['package'], files=files,
        counts=outcome['budget']['counts'], seconds=outcome['seconds'],
        native_gateway_recovered_without_model_restart=True, nvml_queries=len(queries))


ORIGINAL_ACCEPTED_RUN = BASE['accepted_run']


def accepted_run(plan, *args):
    configure_plan_limits(plan)
    result = ORIGINAL_ACCEPTED_RUN(plan, *args)
    if 'reused_tls' in plan:
        require(reuse_tls(plan['reused_tls']['directory']) == plan['reused_tls'],
                'original TLS observations changed')
        result['reused_tls'] = plan['reused_tls']
        result['original_continuous_tls_driver_passed'] = False
    return result


def check_business(out, *, verify_cancel=True):
    from tools.native_asr import auxiliary_service
    read = lambda n: json.loads((out/n).read_text())
    plan = json.loads((out.parent/'plan.json').read_text())
    auxiliary = plan.get('audio_auxiliary')
    config = read('final-config.wire')
    if auxiliary:
        clip = plan['auxiliary_clip']
        audio = Path(clip['path']).read_bytes()
        require(hashlib.sha256(audio).hexdigest() == clip['sha256'], 'managed audio source drift')
    tls, version = plan['scenario'].startswith('tls'), plan['scenario'] == 'tool_version'
    completed = Path(plan['reused_tls']['directory'])/'execution' if 'reused_tls' in plan else out
    completed_read = lambda n: json.loads((completed/n).read_text())
    business_config = completed_read('final-config.wire') if \
        plan.get('reused_tls', {}).get('tts_complete') else config
    results = {}
    for label in (['python-zh-json', 'chunked-zh'] if tls else
                  ['primary-asr', 'fallback-asr'] if version else ['primary-asr']):
        raw = ((completed if tls else out)/(label+'.wire')).read_bytes()
        answer = stream_value(raw) if label == 'fallback-asr' and not auxiliary else json.loads(raw)
        if auxiliary:
            results[label] = auxiliary_service.check(answer, auxiliary, business_config, audio)
        else:
            direct().usage(answer);results[label] = direct().quality(answer['text'], 'zh')
    if tls:
        answer = stream_value((completed/'js-en-stream.wire').read_bytes())
        results['js-en-stream'] = auxiliary_service.check(answer, auxiliary, business_config, audio, stream=True) \
            if auxiliary else direct().quality(answer['text'], 'en')
        require(completed_read('beta-file-limit-http.json')['status'] == 413 and
            completed_read('unauthorized-http.json')['status'] == 401, 'scope/upload guards failed')
        for label in ('python-zh-json', 'js-en-stream'):
            sdk = completed_read(label+'.sdk.json')
            require(sdk['ok'] and len(sdk['exchanges']) == 1, 'SDK retried or failed')
            if label == 'python-zh-json':
                require(sdk['version'] == '2.8.1' and
                    sdk['result']['text'] == completed_read(label+'.wire')['text'], 'Python SDK text differs')
                if auxiliary:
                    auxiliary_service.check(sdk['result'], auxiliary, business_config, audio)
                else:
                    direct().usage(sdk['result'], sdk=True)
            else:
                events = [json.loads(line[6:]) for line in (completed/(label+'.wire')).read_text().splitlines()
                          if line.startswith('data: {')]
                require(sdk['version'] == '7.47.0' and sdk['result']['events'] == events,
                        'JS SDK event projection differs')
        if verify_cancel:
            cancelled = read('cancel-observation.json')
            require(cancelled['after']['q4t_requests_aborted_total'] ==
                cancelled['before']['q4t_requests_aborted_total']+1,
                'native disconnect failed to cancel actual transcription')
    answer = read('core-after.wire')['choices'][0]
    require(answer['finish_reason'] == 'stop' and
        answer['message']['content'].strip() == '42', 'core failed after ASR')
    health = config['native_asr_health']
    expected = json.loads((Path(plan['package']['package'])/'engine/manifest.json').read_text())
    states = ('ready', 'metadata_ready') if plan.get('reused_tls', {}).get('tts_complete') else ('ready',)
    require(health['state'] in states and health['reason'] == 'none' and
        health['compute_failures'] == 0 and
        health['core_readiness_dependency'] is False and
        config['native_asr_limits'] == expected['settings']['native_asr']['limits'],
        'ASR health or configured budget lost')
    if plan.get('chat_audio'):
        from tools.serve_recovery import profile_evidence
        from tools.examples.chat_client import read_sse
        clip = plan['clips']['zh']
        require(hashlib.sha256(Path(clip['path']).read_bytes()).hexdigest() == clip['sha256'],
                'managed Chat audio source changed')
        encoded = base64.b64encode(Path(clip['path']).read_bytes()).decode()
        identity = dict(native_asr=expected['settings']['native_asr'])
        for label in (['chat-python', 'chat-js'] if tls else
                      ['primary-chat-audio', 'fallback-chat-audio'] if version else
                      ['primary-chat-audio']):
            stream = label == 'chat-js'
            body = completed_read(label+'-request.json') if tls else read(label+'-request.json')
            require(body == profile_evidence.chat_audio_request(encoded, stream=stream),
                    'managed Chat audio original request differs')
            wire = ((completed if tls else out)/(label+'.wire')).read_bytes()
            if stream:
                parsed = read_sse(io.BytesIO(wire))
                answer = dict(object='chat.completion', choices=parsed.choices, usage=parsed.usage)
            else: answer = json.loads(wire)
            witness = profile_evidence.chat_audio_witness(encoded, answer, business_config, stream=stream)
            profile_evidence.chat_audio_business(witness, identity, encoded, stream=stream)
            results[label] = dict(request_sha256=witness['request_sha256'],
                source_sha256=witness['source_sha256'], usage=answer['usage'])
            if tls:
                sdk = completed_read(label+'.sdk.json')
                require(sdk['ok'] and len(sdk['exchanges']) == 1,
                        'managed Chat SDK failed or retried')
                if stream:
                    events = [json.loads(line[6:]) for line in wire.decode().splitlines()
                              if line.startswith('data: {')]
                    require(sdk['version'] == '7.47.0' and sdk['result']['events'] == events,
                            'managed Chat SSE differs from SDK')
                else:
                    sdk_witness = dict(witness, response=sdk['result'])
                    profile_evidence.chat_audio_business(sdk_witness, identity, encoded)
                    require(sdk['version'] == '2.8.1', 'managed Chat Python SDK drift')
    from tools.native_tts import managed_service
    results.update(managed_service.check(completed if tls else out, plan,
                                         expected['settings'].get('native_tts')))
    continuity = read('core-continuity.json')
    before, after = continuity['before'], continuity['after']
    fields = ('model_pid', 'model_start_ticks', 'controller_pid', 'controller_start_ticks')
    if version:
        require(all(after[role+'_generation'] > before[role+'_generation'] and
                    after[role+'_pid'] != before[role+'_pid']
                    for role in ('controller', 'model', 'gateway')) and
            after['guardian_pid'] == before['guardian_pid'] and
            after['version_recovery']['selected'] == 'fallback' and
            after['version_recovery']['probe_count'] == 1, 'whole-payload recovery missing')
        state = read('profile-continuity.json')
        if plan.get('observe_cold_profiles'):
            from tools.native_asr import recovery_profiles
            registry = read_registry(plan)
            observed = cold_observations(out, plan)
            verified = recovery_profiles.continuity(
                state['before'], state['after'], registry, observed,
                after['model_generation'])
            require(state['verification'] == verified,
                    'profile continuity observation changed')
        else:
            require(state['before'] == state['after'], 'recovery erased original profile budget')
        require(read('stored-before.wire')['choices'] == read('stored-after.wire')['choices'],
                'stored Chat lost on recovery')
    else:
        require(all(before[k] == after[k] for k in fields), 'healthy core restarted')
        if tls and 'reused_tls' not in plan:
            require(after['gateway_generation'] == before['gateway_generation']+1 and
                after['gateway_pid'] != before['gateway_pid'], 'native gateway did not recover')
    return dict(results=results, native_tls=tls, version_recovery=version,
                health=health, actual_core_text='42', default_replaced=False,
                reused_completed=plan.get('reused_tls'),
                cancellation='verified' if tls and verify_cancel else 'pending' if tls else 'not_applicable')


def business_record(version):
    return dict(status='PASS', actual_transcription=True, original_budget=True,
        version_recovery=version, default_replaced=False, automatic_request_or_tool_replay=False)


def read_registry(plan):
    package = Path(plan['package']['package'])
    return json.loads((package/'manifest.json').read_text())['recovery_profiles']


def cold_observations(out, plan):
    path = out/'cold-profiles-fallback.jsonl'
    if not path.exists():
        return []
    require(path.stat().st_size <= 1024**2, 'cold observation file bound')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    require(len(rows) <= 128, 'cold observation count bound')
    package = Path(plan['registration']['payloads']['fallback']['descriptor']['package'])
    expected = (package/'source/tools/serve_recovery/profile_memory.py').resolve()
    require(all(Path(row['module']).resolve() == expected for row in rows),
            'cold observation did not use the selected sealed policy')
    return rows


class Stage(BASE['Stage']):
    def cancel_running(self, connection, label, before):
        # Diarization may emit its first delta only after all model work has
        # finished. Observe live work instead of treating that delta as proof.
        from tools.serve_ops import transport
        deadline = time.monotonic()+30
        observations = []
        while time.monotonic() < deadline:
            self.budget.reserve('controls', label+'-active')
            response = transport.get(self.policy['upstream_operations_port'], '/metrics', timeout=1)
            require(response['http_status'] == 200, 'cancel observation unavailable')
            values = metric_values(response['body'])
            active = (values['q4t_active_chats'] == 1 and
                values['q4t_seq_slots_free'] == 0 and values['q4t_seq_slots_total'] == 1 and
                values['q4t_requests_total'] == before['q4t_requests_total']+1 and
                values['q4t_execution_steps_total'] > before['q4t_execution_steps_total'])
            observations.append({k:values[k] for k in ('q4t_active_chats', 'q4t_seq_slots_free',
                'q4t_seq_slots_total', 'q4t_requests_total', 'q4t_execution_steps_total')})
            save(self.output/(label+'-active.json'), dict(before=before,
                observations=observations[-64:], active_execution_observed=active))
            if active:
                require(connection.sock is not None, 'cancel socket already closed')
                connection.sock.shutdown(socket.SHUT_RDWR)
                return observations[-1]
            time.sleep(.02)
        raise TimeoutError('actual execution was not observed before cancellation')

    def version_ready(self, generation, name):
        if generation == 1 or not self.plan.get('observe_cold_profiles'):
            return VERSION['Stage'].version_ready(self, generation, name)
        self.wait(lambda s:s['model_generation'] == generation and s['business_ready'])
        current = self.family('profiles')
        deadline = time.monotonic()+5
        while current['pending'] is not None and time.monotonic() < deadline:
            time.sleep(.05)
            current = self.family('profiles')
        from tools.native_asr import recovery_profiles
        verified = recovery_profiles.continuity(self.profile_before, current,
            self.registry, cold_observations(self.output, self.plan), generation)
        state = MANAGED['Stage'].ready(self, generation, current['selected'])
        selected = state['version_recovery']
        expected = self.plan['registration']['payloads'][name]
        require(selected['selected'] == name and not selected['pending'] and
            selected['payload_manifest_sha256'] == expected['descriptor']['manifest_sha256'] and
            not selected['protected'], 'unexpected runtime recovery payload')
        save(self.output/'profile-continuity.json', dict(
            before=self.profile_before, after=current, verification=verified))
        return state

    def initialize_state(self):
        return VERSION['Stage'].initialize_state(self) if self.plan['scenario'] == 'tool_version' \
            else LEGACY['Stage'].initialize_state(self)

    def connection(self):
        if self.plan['scenario'].startswith('tls'):
            context = ssl.create_default_context(cafile=self.plan['tls_inputs']['ca']['path'])
            return http.client.HTTPSConnection('127.0.0.1', self.port, timeout=120, context=context)
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)

    def request(self, label, body=None, *, client='alpha', path='/v1/models', method=None,
                expected=200, model_work=False, raw_result=False,
                content_type='application/json', chunked=False, cancel=False, cancel_baseline=None):
        time.sleep(.22)
        self.budget.reserve('chats' if model_work else 'controls', label)
        raw = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        if body is not None and not isinstance(body, bytes):save(self.output/(label+'-request.json'), body)
        connection = self.connection()
        try:
            headers = {'Authorization':'Bearer '+self.keys[client], 'Content-Type':content_type}
            wire_body = (raw[i:i+4096] for i in range(0, len(raw), 4096)) if chunked else raw
            connection.request(method or ('GET' if body is None else 'POST'), path,
                wire_body, headers, encode_chunked=chunked)
            if cancel and cancel_baseline is not None:
                observation = self.cancel_running(connection, label, cancel_baseline)
                (self.output/(label+'.wire')).write_bytes(b'')
                save(self.output/(label+'-http.json'), dict(status=None, cancel=True,
                    response_received=False, condition='observed_active_execution', observation=observation))
                return b''
            response = connection.getresponse()
            if cancel:
                require(response.status == 200, 'cancel request did not stream')
                wire = b''
                while len(wire) < 1024**2:
                    line = response.readline(65537)
                    require(line and len(line) <= 65536, 'stream ended before cancellation')
                    wire += line
                    if line.startswith(b'data: {'):
                        event = json.loads(line[6:])
                        if event.get('type') == 'transcript.text.delta' and event['delta']:break
                else:raise RuntimeError('cancellation byte budget')
                if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                response.close()
            else:wire = response.read(4*1024**2+1)
            require(len(wire) <= 4*1024**2, 'response byte budget')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), chunked=chunked, cancel=cancel))
            require(response.status == expected, 'unexpected HTTP status: '+label)
            return wire if raw_result or cancel else json.loads(wire)
        finally:connection.close()

    def audio(self, label, *, stream=False, client='alpha', expected=200, chunked=False,
              cancel=False, language='zh', cancel_baseline=None):
        boundary = 'q4t-native-asr-service'
        fields = [('model', 'qwen3-asr-0.6b'), ('include[]', 'logprobs')]
        if stream:fields.append(('stream', 'true'))
        if self.plan.get('audio_auxiliary'):
            from tools.native_asr import auxiliary_service
            fields = auxiliary_service.fields(auxiliary_service.request(
                self.plan['audio_auxiliary'], stream))
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                  f'{value}\r\n').encode() for key, value in fields]
        clip = self.plan.get('auxiliary_clip', self.plan['clips'][language])
        audio = Path(clip['path']).read_bytes()
        require(hashlib.sha256(audio).hexdigest() == clip['sha256'], 'managed audio drift')
        parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),
                  audio, f'\r\n--{boundary}--\r\n'.encode()]
        return self.request(label, b''.join(parts), client=client,
            path='/v1/audio/transcriptions', model_work=True, expected=expected,
            content_type='multipart/form-data; boundary='+boundary,
            raw_result=stream, chunked=chunked, cancel=cancel, cancel_baseline=cancel_baseline)

    def sdk(self, label, language, python=False):
        self.budget.reserve('chats', label)
        config = dict(base_url=f'https://127.0.0.1:{self.port}/v1',
            api_key=self.keys['alpha'], ca=self.plan['tls_inputs']['ca']['path'],
            audio=self.plan.get('auxiliary_clip', self.plan['clips'][language])['path'], filename=language+'.wav',
            sdk_directory=self.plan['sdk']['sdk_directory'],
            request=self.plan['requests']['zh' if python else 'en'])
        save(self.output/(label+'-request.json'), {k:v for k,v in config.items() if k != 'api_key'})
        command = [self.plan['python_sdk']['python'], '-I', '-B',
                   str(ROOT/'tools/native_asr/transcription_sdk.py')] if python else \
                  [self.plan['sdk']['node']['path'], str(ROOT/'tools/native_asr/transcription_sdk.mjs')]
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
             'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env.update(PYTHONDONTWRITEBYTECODE='1', NODE_EXTRA_CA_CERTS=config['ca'])
        run = subprocess.run(command, input=json.dumps(config), capture_output=True,
                             text=True, env=env, timeout=190)
        (self.output/(label+'.stderr')).write_text(run.stderr)
        require(run.stdout, 'SDK returned no evidence')
        value = json.loads(run.stdout)
        (self.output/(label+'.wire')).write_bytes(base64.b64decode(value.pop('wire_base64'), validate=True))
        save(self.output/(label+'.sdk.json'), value)
        require(run.returncode == 0 and value['ok'] and len(value['exchanges']) == 1,
                'actual native TLS SDK failed: '+label)

    def chat_audio(self, label, *, sdk=False, stream=False):
        from tools.serve_recovery import profile_evidence
        clip = self.plan['clips']['zh']
        raw = Path(clip['path']).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == clip['sha256'], 'managed Chat source drift')
        body = profile_evidence.chat_audio_request(base64.b64encode(raw).decode(), stream=stream)
        if not sdk:
            return self.request(label, body, path='/v1/chat/completions', model_work=True)
        self.budget.reserve('chats', label)
        save(self.output/(label+'-request.json'), body)
        config = dict(base_url=f'https://127.0.0.1:{self.port}/v1',
            api_key=self.keys['alpha'], ca=self.plan['tls_inputs']['ca']['path'],
            sdk_directory=self.plan['sdk']['sdk_directory'], request=body)
        command = ([self.plan['sdk']['node']['path'], str(ROOT/'tools/native_asr/chat_sdk.mjs')]
                   if stream else [self.plan['python_sdk']['python'], '-I', '-B',
                                   str(ROOT/'tools/native_asr/chat_sdk.py')])
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
             'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env.update(PYTHONDONTWRITEBYTECODE='1', NODE_EXTRA_CA_CERTS=config['ca'])
        run = subprocess.run(command, input=json.dumps(config), capture_output=True,
                             text=True, env=env, timeout=190)
        (self.output/(label+'.stderr')).write_text(run.stderr)
        require(run.stdout, 'Chat SDK returned no evidence')
        value = json.loads(run.stdout)
        (self.output/(label+'.wire')).write_bytes(base64.b64decode(value.pop('wire_base64'), validate=True))
        save(self.output/(label+'.sdk.json'), value)
        require(run.returncode == 0 and value['ok'] and len(value['exchanges']) == 1,
                'actual native TLS Chat SDK failed: '+label)

    def drained(self, label):
        from tools.serve_ops import transport
        end = time.monotonic()+5
        observations = []
        while time.monotonic() < end:
            self.budget.reserve('controls', label)
            response = transport.get(self.policy['upstream_operations_port'], '/metrics', timeout=1)
            require(response['http_status'] == 200, 'metrics unavailable')
            values = metric_values(response['body'])
            observations.append(dict(response=response, values=values))
            save(self.output/(label+'.json'), observations)
            if (values['q4t_seq_slots_free'] == values['q4t_seq_slots_total'] == 1 and
                all(values[k] == 0 for k in ('q4t_active_chats',
                    'q4t_structured_cleanup_pending', 'q4t_structured_workers_active',
                    'q4t_structured_active', 'q4t_structured_preparing')) and
                values['q4t_structured_workers_started_total'] ==
                    values['q4t_structured_workers_reaped_total']):
                return values
            time.sleep(.05)
        raise TimeoutError('owned ASR request did not drain')

    def configuration(self, label='final'):
        from tools.serve_ops import transport
        self.budget.reserve('controls', label+'-config')
        response = transport.get(self.policy['upstream_operations_port'],
                                 '/v1/config', timeout=2)
        save(self.output/(label+'-config-http.json'), response)
        (self.output/(label+'-config.wire')).write_text(response['body'])
        require(response['http_status'] == 200, 'private configuration unavailable')

    def exercise(self):
        from tools.native_tts import managed_service
        version, tls = self.plan['scenario'] == 'tool_version', self.plan['scenario'].startswith('tls')
        first = self.version_ready(1, 'primary') if version else self.ready(1, 'primary')
        self.request('capabilities', path='/v1/capabilities')
        if tls:
            if 'reused_tls' not in self.plan:
                self.request('unauthorized', client='invalid', expected=401)
                managed_service.observe(self, 'tls-before')
                self.sdk('python-zh-json', 'zh', python=True)
                if self.plan.get('chat_audio'): self.chat_audio('chat-python', sdk=True)
                self.kill(first, 'gateway', 'native-asr-gateway-exit')
                self.wait(lambda s:s['business_ready'] and
                          s['gateway_generation'] == first['gateway_generation']+1)
                managed_service.observe(self, 'tls-after')
                self.sdk('js-en-stream', 'en')
                if self.plan.get('chat_audio'): self.chat_audio('chat-js', sdk=True, stream=True)
                self.audio('beta-file-limit', client='beta', expected=413)
                self.audio('chunked-zh', chunked=True)
            before = self.drained('before-cancel')
            self.audio('cancel-stream', stream=True, cancel=True, language='en', cancel_baseline=before)
            after = self.drained('after-cancel')
            save(self.output/'cancel-observation.json', dict(before=before, after=after))
        else:
            managed_service.observe(self, 'primary')
            self.audio('primary-asr')
            if self.plan.get('chat_audio'): self.chat_audio('primary-chat-audio')
        if version:
            self.request('stored-before', dict(self.plan['requests']['chat'], store=True),
                         path='/v1/chat/completions', model_work=True)
            fields = ('history', 'pending', 'selected', 'not_before_ns', 'light_since_ns')
            before = self.family('profiles')
            if self.plan.get('observe_cold_profiles'):
                self.profile_before = before
                save(self.output/'profile-before-recovery.json', before)
            self.fatal_gateway(first)
            self.version_ready(2, 'fallback')
            # Exercise alignment after a whole-tree fallback as well. The TLS
            # scenario separately verifies actual VAD SSE through the SDK.
            managed_service.observe(self, 'fallback')
            self.audio('fallback-asr', stream=not bool(self.plan.get('audio_auxiliary')))
            if self.plan.get('chat_audio'): self.chat_audio('fallback-chat-audio')
            stored = json.loads((self.output/'stored-before.wire').read_text())
            self.request('stored-after', path='/v1/chat/completions/'+stored['id'])
            after = self.family('profiles')
            if not self.plan.get('observe_cold_profiles'):
                save(self.output/'profile-continuity.json', dict(
                    before={k:before[k] for k in fields}, after={k:after[k] for k in fields}))
        self.request('core-after', self.plan['requests']['chat'], path='/v1/chat/completions',
                     client='beta' if tls else 'alpha', model_work=True)
        self.configuration()
        after = self.version_ready(2, 'fallback') if version else self.ready(1, 'primary')
        save(self.output/'core-continuity.json', dict(before=first, after=after))
        save(self.output/'media-usage.json', check_business(self.output))
        save(self.output/'business-final.json', business_record(version))


GLOBALS.update(__file__=__file__, LIMITS=LIMITS, requests=requests, prepare=prepare,
               Stage=Stage, check_business=check_business, business_record=business_record,
               accepted_run=accepted_run,
               PREPARE_PATH_ARGUMENTS=('separator-audio',),
               ACCEPTANCE_PREFIX='NATIVE_ASR_SERVICE_')
for function in (VERSION['launcher'], MANAGED['launcher']):
    function.__globals__['__file__'] = __file__
from tools.native_asr import recovery_profiles
VERSION['actor'].__globals__['PROFILE_LAUNCH_OBSERVER'] = recovery_profiles.observe

if __name__ == '__main__':
    if '--plan' in sys.argv:
        configure_plan_limits(json.loads(Path(sys.argv[sys.argv.index('--plan')+1]).read_text()))
    BASE['main']()
