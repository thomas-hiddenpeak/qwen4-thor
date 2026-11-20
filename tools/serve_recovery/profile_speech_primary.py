"""Retain exact completed primary work after a checker-only optional-field error.

The original stage and lease stay FAILED/COMMAND_FAILED. Every captured
business result and both cleanup paths must independently validate before
continuing only the unexecuted light capacity. No additional primary sampling.
"""
import copy
import json
from pathlib import Path

from tools.serve_recovery import ledger, profile_evidence, profile_realtime
from tools.serve_recovery import profile_speech_evidence as speech
from tools.serve_service.package import read, require, sha

FILES = {'cleanup.json': '53f4fa6ffc45e488b5b33c798625350af2509e329d1de73030787555ffd731cb',
 'lease/exit.json': 'd43fea08c610f2650c46d54043dfacbe810ce8c34562e1f4a0d93467f91ecd9d',
 'lease/nvml-queries.jsonl': 'd5160c56d8d39ea16758080790d4c549f858551f5388da8d1e56eb3d4636ad76',
 'plan.json': '37e15c3a4401f2232bc7331efff2b0d0c826d3e007ddbc9bad90b8bcbb298493',
 'primary-capabilities.json': 'a7014745b1c71f0b6f989b06e18c2e2011fac42c3119271ea089bd8cb5b3f95d',
 'primary-command.json': 'c19a44d2313a8e2862a8027ce8c1e8ee9821848f5e3a30c00b6581de2240d901',
 'primary-config.json': 'b476005cd5fa2e81c430a7dbb0c133a8e8fd6ef5df680744a7644b23ae2bdd45',
 'primary-identity.json': '4f5608fe6590f042daf03be70ce37af4cc22e667b3ca617150c1aac0479a98e1',
 'primary-shared-chat-speech.http.json': 'ad6753ee772e4dcba38f45e764daf5a59cad6e9c04a295455cf644fc33c526b1',
 'primary-shared-chat-speech.request.json': '1a28abd38c6b8f484f3d318be8e8bc564d5713418efc5dd8dba242649266ba07',
 'primary-shared-chat-speech.wire': '86b85d901987dd2688762bbc5e9e7bed0916d30e7aa90debcad17be2ed3e7d2d',
 'primary-speech-builtin-after-0.wire': '8491cfad64428cece68228107e5d75ec88704ae79c0880ddad1b431fdbb2c77a',
 'primary-speech-builtin-before.wire': '0c3c4ddbf51a44d976d99a02c51ad4fb39ba0f18874a92678240fdea0f73fd9b',
 'primary-speech-builtin.sdk.json': 'ad66a783f16222d2dcc5e19ba62a0cccd95fc64f99148b3e1c3b1073834c6314',
 'primary-speech-custom-after-0.wire': 'e9da950ea2595d024dd365dc48866d93103ea5a8cb6c9b4a5726405b78b9f8c5',
 'primary-speech-custom-before.wire': '0b8999d53b78b4061069b935997dca6f5e9239469ca7bf6edddbdff5697c24c5',
 'primary-speech-custom.sdk.json': 'e3e2325adaa515fbe84828e86a6673661d97e95fad539306ce35a4d3d39afb66',
 'primary-speech-entry-cleanup.json': 'e7c411cb4235f7fc90c548f46ef72b4e656da0d657b9bdc1e4271d2124c5e3f8',
 'primary-speech-foreign-after-0.wire': 'eea253d8bb485f161a74d38f7d91673d62befbf42cce9970733f2524a4f7cdc9',
 'primary-speech-foreign-before.wire': 'd12c7e7814fe6623f341648623c368e0a43732241726c35284553cc32ca8ce02',
 'primary-speech-foreign.sdk.json': '501d3ab3f242531bf4522f6b759a067e5130d00971cf42acffbc7ac76dab0b3f',
 'primary-speech-observed.json': '39ca58dc2c7098d1f7054321e3bf4419e47ade8307269c1964f4e87450825ae7',
 'primary-tts-health.http.json': '9c82123f2835ba79ab2b9c17a77ded28c1edd657a6b2017d7c3bc29d52ba491b',
 'primary-tts-health.wire': 'c84f20e5e96a164ebec92dd7a39b16a9d16305dc00c9849588aad1110033d02d',
 'result.json': 'af7a28b56a8037933e5957382ab4e1c5bb8db3a87443b0180b679057fe6c6a41'}


def validate_result(result, lease, queries, cleanup, entry):
    require(result['status'] == 'FAILED' and result['error'].endswith("KeyError: 'voice'\n") and
        result['counts'] == dict(starts=1,chats=5,controls=43,websocket_sessions=3,ingress_starts=1) and
        result['binary_sha256'] == speech_source()['candidate_binary'] and
        0 < result['seconds'] < 1200 and lease['status'] == 'COMMAND_FAILED' and
        lease['child_returncode'] == 1 and lease['compute_backend'] == 'nvml' and
        lease['lease_inherited_by_child'] is True and
        len(queries) == 78 and all(q.get('status') == 'OK' and q.get('returncode') == 0 for q in queries),
        'not the reviewed checker-only primary interruption')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True and
        entry['exit_code'] == 0 and entry['port_released'] is True, 'primary cleanup incomplete')


def speech_source():
    from tools.serve_recovery.profile_speech_source import REVIEW
    return REVIEW


def verify(path, plan, old):
    path = Path(path).resolve(strict=True)
    require(path.is_dir() and not path.is_symlink() and
        all(sha(path/name) == digest for name,digest in FILES.items()),
        'reviewed primary source bytes changed')
    source = read(path/'plan.json')
    require(source['identity'] == plan['identity'] and source['profiles'] == plan['profiles'] and
        source['binary_sha256'] == plan['binary_sha256'], 'primary continuation identity changed')
    queries=[json.loads(line) for line in (path/'lease/nvml-queries.jsonl').read_text().splitlines()]
    validate_result(read(path/'result.json'),read(path/'lease/exit.json'),queries,
                    read(path/'cleanup.json'),read(path/'primary-speech-entry-cleanup.json'))
    witness=read(path/'primary-speech-observed.json')
    for mode,row in witness['sessions'].items():
        require(row['session'] == read(path/f'primary-speech-{mode}.sdk.json'), 'SDK observation changed')
        client='audio' if mode=='custom' else profile_realtime.encrypted.CLIENT
        for side,suffix in [('before','before'),('after','after-0')]:
            metrics=(path/f'primary-speech-{mode}-{suffix}.wire').read_bytes()
            require(row[side] == profile_realtime.state(
                profile_realtime.encrypted.foreground.base.lifecycle.parse_metrics(metrics),client),
                'observed rate state differs from actual metrics')
    speech.business(witness)
    for name in ('primary-shared-chat-speech','primary-tts-health'):
        require(read(path/(name+'.http.json'))['status'] == 200, 'missing actual shared Chat or TTS health')
    require(read(path/'primary-shared-chat-speech.request.json') == profile_evidence.tts_requests()['chat'],
            'actual shared Chat request differs')
    tts=copy.deepcopy(old['business']['tts'])
    tts.update(chat_response=read(path/'primary-shared-chat-speech.wire'),
               health=read(path/'primary-tts-health.wire')['native_tts_health'])
    fresh=dict(tts=tts,realtime_speech=witness)
    business=dict(copy.deepcopy(old['business']),**fresh)
    profile_evidence.business(business,plan['identity'],plan['profiles']['primary'],
                              read(path/'primary-capabilities.json'))
    return fresh


def environment(path, current):
    # The current argument already passed the new light lease's strict checks.
    # Preserve both original exits, never rewrite COMMAND_FAILED to COMPLETED.
    path=Path(path)
    proof=dict(format='q4t-speech-two-lease-environment-v1',
        primary_exit_sha256=FILES['lease/exit.json'], primary_exit_status='COMMAND_FAILED',
        primary_cause='checker_required_optional_response_voice', primary_business_review='PASS',
        light_exit_sha256=current['lease_result_sha256'], light_exit_status='COMPLETED',
        nvml_queries=78+current['nvml_queries'], cleanup_complete=True)
    return dict(status='PASS',env_abort=False,all_queries_ok=True,cleanup_complete=True,
                nvml_queries=proof['nvml_queries'],lease_result_sha256=ledger.digest(proof)), proof


def light_environment(lease, queries, cleanup):
    require(lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0 and
        lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'] is True and
        queries and all(q.get('status') == 'OK' and q.get('returncode') == 0 for q in queries),
        'continued light GPU environment not accepted')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True,
        'continued light cleanup incomplete')
    return dict(status='PASS',env_abort=False,all_queries_ok=True,cleanup_complete=True,
                nvml_queries=len(queries))
