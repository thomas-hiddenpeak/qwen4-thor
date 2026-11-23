"""Snapshot the accepted text release and its operational dependencies."""
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
LEGACY_SOURCES = (
    'tools/serve_service/package.py', 'tools/serve_service/supervisor.py',
    'tools/serve_service/backend.py',
    'tools/serve_service/service.py', 'tools/evalscope/gpu_exclusive.py',
    'tools/evalscope/bounded_log.py',
    'tools/evalscope/nvml_process_query.py', 'tools/release/release.py',
    'tools/serve_ops/contracts.py', 'tools/serve_ops/identity.py',
    'tools/serve_ops/transport.py',
)
V2_SOURCES = (*LEGACY_SOURCES, 'tools/serve_service/memory.py')
SOURCES = (*V2_SOURCES, 'tools/serve_service/inspection.py')
V4_SOURCES = (*SOURCES, 'tools/serve_gateway/client_policy.py')
V5_SOURCES = (*V4_SOURCES, 'tools/serve_service/resources.py')
V7_SOURCES = (*V5_SOURCES, 'tools/serve_service/telemetry.py')
V8_SOURCES = (*V7_SOURCES, 'tools/serve_service/nvml_policy.py',
              'tools/serve_service/monitor_control.py')
V9_SOURCES = (*V8_SOURCES, 'tools/serve_service/auxiliary.py')
V10_SOURCES = V9_SOURCES  # Same inventory; new resource-control contract.
V11_SOURCES = (*V10_SOURCES, 'tools/serve_service/capacity.py')
FILES = ('q4t', 'q4t.release.json', 'CMakeCache.txt', 'acceptance.json')
APPLICATION_LIMITS = dict(max_object_mib=16, max_client_mib=64, max_total_mib=256,
    max_client_objects=1000, max_total_objects=10000, io_timeout_ms=2000)
# Absent in historical x-vector-only releases. Keep this behavior declaration
# in the recovery contract: an older engine must not silently discard ICL.
VOICE_CONDITIONING = dict(profile='q4t-registered-voice-icl-v1',
    reference_format='q4t.tts-reference.v1', reference_sample_rate=24000,
    max_reference_frames=750, code_groups=16, codebook_size=2048,
    reference_transcript='literal_context',
    input_usage='text_and_all_reference_codes',
    legacy_records='speaker_embedding_only',
    invalid_reference='reject_without_fallback')
NOTICE_ROOT = next((p for p in (ROOT/'third_party/licenses',
    ROOT.parent/'engine/licenses', ROOT.parent/'licenses') if p.is_dir()),
    ROOT/'third_party/licenses')
NOTICE_FILES = tuple(sorted(str(p.relative_to(NOTICE_ROOT))
    for p in NOTICE_ROOT.rglob('*') if p.is_file()))
DECLARATION_PROFILE = 'q4t-release-declarations-v1'
COMPLETION_DECLARATION_PROFILE = 'q4t-release-declarations-v2'
APPLICATION_DECLARATION_PROFILE = 'q4t-release-declarations-v3'
DECLARATION_SCHEMAS = ('config', 'message', 'preflight', 'reasoning',
                       'response', 'stop', 'thinking')
DECLARATION_FILES = ('capabilities.json', 'configuration.json',
                     *('schemas/'+s+'.json' for s in DECLARATION_SCHEMAS))
DECLARATION_SECTIONS = ('cancellation', 'errors', 'http', 'memory_admission',
                        'observability', 'preflight', 'reasoning',
                        'structured_output', 'template', 'text_stop')
# Some releases combine preserved failed attempts and targeted successful
# repairs into an explicit acceptance decision. Do not accept arbitrary
# ACCEPT_* or turn a failed underlying attempt into a PASS_* receipt.
AGGREGATE_ACCEPTANCES = frozenset({'ACCEPT_BOUNDED_CLIENT_ISOLATION'})
PROFILE = dict(name='text-mtp-managed-v1', host='127.0.0.1', max_seq=1,
    max_len=8192, max_prefill=2048, max_tokens=256, mtp_k=3,
    max_inflight_requests=8, max_queued_requests=2,
    min_available_memory_mib=2048, memory_recovery_margin_mib=512,
    startup_memory_headroom_mib=2048)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_python_runtime():
    require(sys.version_info >= (3, 11), 'service Python requires version 3.11 or later')
    names = ('memfd_create', 'MFD_CLOEXEC', 'MFD_ALLOW_SEALING',
             'pidfd_open', 'waitid', 'P_PIDFD')
    missing = [name for name in names if not hasattr(os, name)]
    require(not missing, 'service Python lacks Linux APIs: '+','.join(missing))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    value = Path(path).lstat()
    require(stat.S_ISREG(value.st_mode), 'regular file required')


def decode_metadata(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate metadata key')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique,
        parse_constant=lambda _: require(False, 'non-finite metadata'))


def read(path):
    regular(path)
    require(Path(path).stat().st_size <= 2*1024**2, 'metadata size limit')
    return decode_metadata(Path(path).read_text())


def save(path, value):
    """Replace our private metadata, including file and directory durability."""
    path = Path(path)
    temp = path.with_name(path.name+'.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def private_directory(path, repository=ROOT):
    path = Path(path).absolute()
    require(path == path.resolve(), 'symlink or relative traversal in output')
    require(any(path.is_relative_to(repository / name)
                for name in ('build', '.q4t-work')), 'output outside workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def add_application_options(parser):
    parser.add_argument('--object-store-dir')
    parser.add_argument('--live-recordings', action='store_true')
    parser.add_argument('--sip-trunks-file', type=Path)
    parser.add_argument('--live-webrtc-address')
    for key in APPLICATION_LIMITS:
        parser.add_argument('--object-'+key.replace('_', '-'), type=int)


def application_cli(args):
    options = {key: getattr(args, 'object_'+key) for key in APPLICATION_LIMITS
               if getattr(args, 'object_'+key) is not None}
    if args.object_store_dir is not None:
        options['directory'] = args.object_store_dir
    if args.live_recordings:
        options['live_recordings'] = True
    return options or None


def application_settings(value):
    require(type(value) is dict and set(value) - {'live_recordings'} ==
        {'profile', 'directory', *APPLICATION_LIMITS}, 'invalid application state settings')
    require(value['profile'] == 'q4t-objects-v1', 'unknown application state profile')
    require(type(value.get('live_recordings', False)) is bool, 'live_recordings must be boolean')
    directory = value['directory']
    require(type(directory) is str and (not directory or
        directory.startswith('/') and len(directory.encode()) <= 4096 and
        '\0' not in directory and any(directory.split('/')) and
        not {'.', '..'}.intersection(directory.split('/'))),
        'application directory must be absolute without dot components')
    require(all(type(value[k]) is int for k in APPLICATION_LIMITS),
            'application state limits must be integers')
    require(1 <= value['max_object_mib'] <= 32 and
        value['max_object_mib'] <= value['max_client_mib'] <=
            value['max_total_mib'] <= 1048576 and
        1 <= value['max_client_objects'] <= value['max_total_objects'] <= 100000 and
        1 <= value['io_timeout_ms'] <= 30000, 'invalid application state limits')
    return dict(value)


NATIVE_EMBEDDING = dict(profile='q4t-native-embedding-v1', model='qwen3-embedding-0.6b',
    revision='97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3',
    weight_sha256='0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd')
NATIVE_ASR = dict(profile='q4t-native-asr-v1', model='qwen3-asr-0.6b',
    revision='5eb144179a02acc5e5ba31e748d22b0cf3e303b0',
    weight_sha256='79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea')
NATIVE_TTS = dict(profile='q4t-native-tts-v1',
    model='qwen3-tts-12hz-1.7b-customvoice',
    revision='0c0e3051f131929182e2c023b9537f8b1c68adfe',
    weight_sha256='38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137',
    codec_weight_sha256='836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258')
NATIVE_TTS_BASE = dict(NATIVE_TTS, model='qwen3-tts-12hz-1.7b-base',
    revision='fd4b254389122332181a7c3db7f27e918eec64e3',
    weight_sha256='38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c')
TTS_LIMITS = dict(max_text_tokens=16384, max_prefill_tokens=8192,
                  max_frames=4096, workspace_mib=8192, max_output_mib=64)
ASR_LIMITS = dict(max_audio_seconds=30, max_context_tokens=4096,
                  max_output_tokens=1024, workspace_mib=1024)
NATIVE_ALIGNMENT = dict(profile='q4t-native-aligner-v1', model='qwen3-forced-aligner-0.6b',
    revision='c7cbfc2048c462b0d63a45797104fc9db3ad62b7',
    weight_sha256='47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7')
ALIGNER_LIMITS = dict(max_audio_seconds=30, max_context_tokens=4096,
                      max_words=512, workspace_mib=1024)
NATIVE_VAD = dict(profile='q4t-native-silero-vad-v1', model='silero-vad-16k',
    revision='066409f3d9db45a30d21dfcbf9147826ffdd7052',
    weight_sha256='c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1')
VAD_LIMITS = dict(max_chunks=64)
NATIVE_SPEAKER = dict(profile='q4t-native-ecapa-speaker-v1',
    model='speechbrain-ecapa-voxceleb',
    revision='0f99f2d0ebe89ac095bcc5903c4dd8f72b367286',
    weight_sha256='901bb7278ba2f4f43076854e1e2757d7ae8c15f429b16b42e35cd729c75754af')
SPEAKER_LIMITS = dict(window_ms=3000, step_ms=750, max_windows=256,
                      max_speakers=16, workspace_mib=128)
NATIVE_SEPARATOR = dict(profile='q4t-native-sepformer-v1',
    model='speechbrain-sepformer-wsj02mix',
    revision='3a2826343a10e2d2e8a75f79aeab5ff3a2473531',
    weight_sha256='5f5cbbd10eae7be4e56b752cbc0d27f02305dd0e5cdc3d9c115b51881ca52299')
SEPARATOR_LIMITS = dict(max_audio_seconds=30, workspace_mib=2048)
ASR_AUXILIARY = dict(alignment=(NATIVE_ALIGNMENT, ALIGNER_LIMITS, 'aligner'),
                     vad=(NATIVE_VAD, VAD_LIMITS, 'vad'),
                     speaker=(NATIVE_SPEAKER, SPEAKER_LIMITS, 'speaker'),
                     separator=(NATIVE_SEPARATOR, SEPARATOR_LIMITS, 'separator'))


def audio_asset_directory(path):
    require(type(path) is str and 1 < len(path.encode('utf-8')) <= 4096 and
            path.startswith('/') and '\0' not in path and
            not {'', '.', '..'}.intersection(path.split('/')[1:]),
            'invalid optional audio asset directory')


def asr_auxiliary_settings(value, *, voice_registration=False):
    require(type(value) is dict and value.keys() <= ASR_AUXILIARY.keys(),
            'invalid optional ASR backend inventory')
    require(type(voice_registration) is bool, 'invalid voice registration context')
    require('speaker' not in value or
            ('vad' in value and ('alignment' in value or voice_registration)),
            'speaker requires VAD and either alignment or TTS Base')
    require('separator' not in value or {'alignment', 'vad', 'speaker'} <= value.keys(),
            'separator requires speaker, alignment and VAD backends')
    result = {}
    for name, selected in value.items():
        fixed, defaults, _ = ASR_AUXILIARY[name]
        require(type(selected) is dict and selected.keys() == {*fixed, 'directory', 'limits'}
                and all(selected[k] == v for k, v in fixed.items()),
                'invalid optional ASR backend identity: '+name)
        audio_asset_directory(selected['directory'])
        limits = selected['limits']
        require(type(limits) is dict and limits.keys() == defaults.keys() and
                all(type(v) is int for v in limits.values()), 'invalid '+name+' limits')
        if name == 'alignment':
            require(1 <= limits['max_audio_seconds'] <= 400 and
                128 <= limits['max_context_tokens'] <= 8192 and
                1 <= limits['max_words'] <= 2048 and
                64 <= limits['workspace_mib'] <= 8192 and
                limits['max_audio_seconds']*13 + 2 + limits['max_words']*3 <=
                    limits['max_context_tokens'], 'invalid alignment capacity')
        elif name == 'vad':
            require(1 <= limits['max_chunks'] <= 256, 'invalid VAD chunk capacity')
        elif name == 'separator':
            require(1 <= limits['max_audio_seconds'] <= 30 and
                    64 <= limits['workspace_mib'] <= 2048,
                    'invalid separator capacity')
        else:
            require(1000 <= limits['window_ms'] <= 30000 and
                100 <= limits['step_ms'] <= limits['window_ms'] and
                1 <= limits['max_windows'] <= 1024 and
                1 <= limits['max_speakers'] <= 64 and
                16 <= limits['workspace_mib'] <= 1024,
                'invalid speaker capacity')
        result[name] = copy.deepcopy(selected)
    # No stat/read of optional assets: only a request may load the backend.
    return result


def asr_auxiliary_observed_limits(configuration, name):
    require(name in ASR_AUXILIARY, 'unknown audio auxiliary limits')
    if name == 'vad':
        return dict(max_chunks=configuration.get('native_vad_max_chunks'))
    return configuration.get('native_'+name+'_limits')


def add_asr_auxiliary_options(parser):
    for _, (_, defaults, prefix) in ASR_AUXILIARY.items():
        parser.add_argument('--'+prefix+'-model-dir', type=Path)
        for option in defaults:
            parser.add_argument('--'+prefix+'-'+option.replace('_', '-'), type=int)


def asr_auxiliary_cli(args):
    result = {}
    for name, (fixed, defaults, prefix) in ASR_AUXILIARY.items():
        directory = getattr(args, prefix+'_model_dir')
        overrides = {k:getattr(args, prefix+'_'+k) for k in defaults
                     if getattr(args, prefix+'_'+k) is not None}
        require(directory is not None or not overrides,
                prefix+' limits require its model directory')
        if directory is not None:
            result[name] = dict(fixed, directory=str(directory),
                                limits=dict(defaults, **overrides))
    return asr_auxiliary_settings(result, voice_registration=
        getattr(args, 'tts_base_model_dir', None) is not None) or None


def embedding_settings(value):
    require(type(value) is dict and value.keys() == {*NATIVE_EMBEDDING, 'directory'}
            and all(value[k] == v for k, v in NATIVE_EMBEDDING.items()),
            'invalid native embedding identity')
    path = value['directory']
    require(type(path) is str and 1 < len(path.encode('utf-8')) <= 4096 and
            path.startswith('/') and '\0' not in path and
            not {'', '.', '..'}.intersection(path.split('/')[1:]),
            'invalid native embedding directory')
    # Asset presence is checked by the isolated endpoint loader. Verification
    # of this optional configuration must not make Chat depend on its files.
    return dict(value)


def asr_settings(value, *, voice_registration=False):
    require(type(value) is dict and
            {*NATIVE_ASR, 'directory', 'limits'} <= value.keys() <=
                {*NATIVE_ASR, 'directory', 'limits', *ASR_AUXILIARY}
            and all(value[k] == v for k, v in NATIVE_ASR.items()),
            'invalid native ASR identity')
    path = value['directory']
    require(type(path) is str and 1 < len(path.encode('utf-8')) <= 4096 and
            path.startswith('/') and '\0' not in path and
            not {'', '.', '..'}.intersection(path.split('/')[1:]),
            'invalid native ASR directory')
    limits = value['limits']
    require(type(limits) is dict and limits.keys() == ASR_LIMITS.keys() and
            all(type(v) is int for v in limits.values()), 'invalid native ASR limits')
    require(1 <= limits['max_audio_seconds'] <= 600 and
            128 <= limits['max_context_tokens'] <= 8192 and
            1 <= limits['max_output_tokens'] <= 4096 and
            64 <= limits['workspace_mib'] <= 8192 and
            limits['max_audio_seconds'] * 13 + 15 + limits['max_output_tokens'] <=
                limits['max_context_tokens'], 'invalid native ASR limits')
    # Optional assets are validated by the isolated request loader. Missing
    # audio files must not make a healthy primary Chat package unverifiable.
    auxiliary = asr_auxiliary_settings(
        {k:v for k,v in value.items() if k in ASR_AUXILIARY},
        voice_registration=voice_registration)
    return dict(value, limits=dict(limits), **auxiliary)


def tts_settings(value, *, base=False):
    fixed = NATIVE_TTS_BASE if base else NATIVE_TTS
    require(type(value) is dict and value.keys() == {*fixed, 'directory', 'limits'}
            and all(value[k] == v for k, v in fixed.items()),
            'invalid native TTS identity')
    audio_asset_directory(value['directory'])
    limits = value['limits']
    require(type(limits) is dict and limits.keys() == TTS_LIMITS.keys() and
            all(type(v) is int for v in limits.values()), 'invalid native TTS limits')
    require(4 <= limits['max_text_tokens'] <= 32768 and
            2 <= limits['max_frames'] <= 4096 and
            8 <= limits['max_prefill_tokens'] <= 32768-limits['max_frames'] and
            128 <= limits['workspace_mib'] <= 16384 and
            1 <= limits['max_output_mib'] <= 128, 'invalid native TTS capacity')
    # Both local checkpoints are checked together by the isolated request
    # loader. An absent TTS asset must not make healthy core Chat unverifiable.
    return copy.deepcopy(value)


def settings(port, restart_limit, critical_memory_mib=1024, operations_port=0,
             nvml_policy='strict', model_names=None, application_state=None,
             capacity_profile=None, native_embedding=None, native_asr=None,
             native_tts=None, native_tts_base=None):
    require(type(nvml_policy) is str and nvml_policy in ('strict', 'degrade'),
            'invalid NVML policy')
    require(type(port) is int and 1024 <= port <= 65535, 'invalid port')
    require(type(restart_limit) is int and 0 <= restart_limit <= 3,
            'restart limit must be between zero and three')
    require(nvml_policy != 'degrade' or restart_limit == 0,
            'NVML degradation requires restart-limit zero')
    require(type(critical_memory_mib) is int and
            1 <= critical_memory_mib <= 1024**2, 'invalid critical memory floor')
    require(type(operations_port) is int and (operations_port == 0 or
            1024 <= operations_port <= 65535 and operations_port != port),
            'invalid operations port')
    result = dict(profile=PROFILE.copy(), port=port, restart_limit=restart_limit,
        poll_seconds=5, startup_seconds=300, unavailable_seconds=30,
        restart_delay_seconds=5, stop_seconds=30, kill_seconds=5,
        log_bytes=4*1024**2, log_files=2,
        critical_memory_mib=critical_memory_mib, memory_poll_seconds=1,
        memory_low_seconds=2, memory_unknown_seconds=5)
    if operations_port:
        result['operations_port'] = operations_port
    if nvml_policy == 'degrade':
        result['nvml_policy'] = nvml_policy
    if model_names is not None:
        require(type(model_names) is dict and
                model_names.keys() == {'name', 'aliases'}, 'invalid model registry')
        aliases = model_names['aliases']
        require(type(aliases) is list and len(aliases) <= 32,
                'model aliases must be a list of at most 32 identifiers')
        names = [model_names['name'], *aliases]
        require(all(type(name) is str and 1 <= len(name.encode('utf-8')) <= 256 and
                    all(ord(c) > 32 and ord(c) != 127 for c in name) for name in names),
                'invalid model identifier')
        require(len(set(names)) == len(names), 'duplicate model identifier')
        result['model_names'] = dict(name=names[0], aliases=list(aliases))
    if capacity_profile is not None:
        from tools.serve_service import capacity
        result['profile'] = capacity.validate(capacity_profile)
    if application_state is not None:
        result['application_state'] = application_settings(application_state)
    if native_embedding is not None:
        selected = embedding_settings(native_embedding)
        names = result.get('model_names', dict(name='qwen3.8-flash-next', aliases=[]))
        require(selected['model'] not in [names['name'], *names['aliases']],
                'native embedding model conflicts with Chat registry')
        result['native_embedding'] = selected
    if native_asr is not None:
        selected = asr_settings(native_asr,
                                voice_registration=native_tts_base is not None)
        names = result.get('model_names', dict(name='qwen3.8-flash-next', aliases=[]))
        require(selected['model'] not in [names['name'], *names['aliases']],
                'native ASR model conflicts with Chat registry')
        result['native_asr'] = selected
    if native_tts is not None:
        selected = tts_settings(native_tts)
        names = result.get('model_names', dict(name='qwen3.8-flash-next', aliases=[]))
        require(selected['model'] not in [names['name'], *names['aliases']],
                'native TTS model conflicts with Chat registry')
        result['native_tts'] = selected
    if native_tts_base is not None:
        selected = tts_settings(native_tts_base, base=True)
        names = result.get('model_names', dict(name='qwen3.8-flash-next', aliases=[]))
        require(selected['model'] not in [names['name'], *names['aliases']],
                'native TTS Base model conflicts with Chat registry')
        require(native_tts is None or
                same_json(result['native_tts']['limits'], selected['limits']),
                'native TTS families must share effective limits')
        result['native_tts_base'] = selected
    return result


def accepted_release(metadata, decision):
    require(type(metadata) is dict and type(decision) is dict,
            'release and acceptance must be objects')
    acceptance, status = metadata.get('acceptance'), decision.get('status')
    require(type(acceptance) is str and acceptance.startswith('ACCEPT_') and
            type(status) is str and
            (status.startswith('PASS_') or
             status in AGGREGATE_ACCEPTANCES and status == acceptance),
            'explicit accepted release required')


def same_json(left, right):
    """JSON equality must distinguish true from 1 and false from 0."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False)


def declaration_schemas(profile):
    require(profile in (DECLARATION_PROFILE, COMPLETION_DECLARATION_PROFILE,
                        APPLICATION_DECLARATION_PROFILE),
            'unknown release declaration profile')
    return DECLARATION_SCHEMAS + (('completion_request', 'completion_response')
        if profile != DECLARATION_PROFILE else ()) + (('application', 'openapi')
        if profile == APPLICATION_DECLARATION_PROFILE else ())


def declaration_files(profile):
    return ('capabilities.json', 'configuration.json',
            *('schemas/'+name+'.json' for name in declaration_schemas(profile)))


def verify_declarations(metadata, directory=None, *, required=False):
    """Check frozen observations, not mutable docs or a live server.

    Old sealed packages/rollback pairs may lack the profile. New preparation
    requires it. A package supplies its own copied directory, so original
    artifact paths are provenance only and need not survive distribution.
    These are observed implementation claims, not new feature acceptance.
    """
    declaration = metadata.get('release_declarations')
    if 'release_declarations' not in metadata:
        require(not required, 'release declarations required for new package')
        return None
    require(type(declaration) is dict and set(declaration) == {
        'profile', 'directory', 'binary_sha256', 'protocol_sha256', 'files'},
        'invalid release declaration fields')
    schemas = declaration_schemas(declaration['profile'])
    require(type(declaration['directory']) is str,
            'invalid declaration directory')
    original = Path(declaration['directory'])
    require(original.is_absolute() and '..' not in original.parts,
            'declaration directory must be absolute')
    root = Path(directory) if directory is not None else original
    require(root == root.resolve(strict=True), 'symlink declaration directory')
    files = declaration['files']
    require(type(files) is dict and set(files) == set(declaration_files(declaration['profile'])),
            'declaration inventory differs')
    for name, digest in files.items():
        path = root/name
        require(path == path.resolve(strict=True), 'symlink declaration file')
        regular(path)
        require(sha(path) == digest, 'declaration file changed: '+name)
    protocol = metadata['chat_protocol']
    require(declaration['binary_sha256'] == metadata['binary_sha256'] and
            declaration['protocol_sha256'] == protocol['protocol_sha256'],
            'declaration release identity differs')
    caps, config = (read(root/name) for name in DECLARATION_FILES[:2])
    if caps.get('completions', {}).get('profile') == 'q4t-completions-v1':
        require(declaration['profile'] in (COMPLETION_DECLARATION_PROFILE,
                                          APPLICATION_DECLARATION_PROFILE),
                'completion declarations required')
    if caps.get('responses', {}).get('profile') == 'q4t-responses-v1' or \
            caps.get('conversations', {}).get('profile') == 'q4t-conversations-v1':
        require(declaration['profile'] == APPLICATION_DECLARATION_PROFILE,
                'application declarations required')
        require(all(section in protocol and section in caps for section in
                    ('responses', 'conversations', 'application_state')),
                'application capability sections incomplete')
    require(type(caps) is dict and type(config) is dict and
            caps.get('protocol_source_sha256') ==
            config.get('protocol_source_sha256') == protocol['protocol_sha256']
            and type(caps.get('model')) is str and
            caps['model'] == config.get('model'),
            'declaration observed identity differs')
    require(set(DECLARATION_SECTIONS) <= caps.keys() & protocol.keys(),
            'declaration sections incomplete')
    for key in caps.keys() & protocol.keys():
        require(same_json(caps[key], protocol[key]),
                'declaration capability differs: '+key)
    require(same_json(config.get('observability_version'),
                      caps['observability']['version']),
            'declaration observation versions differ')
    require(same_json(metadata.get('observed_configuration'), config),
            'declaration configuration differs')
    for name in schemas:
        relative = 'schemas/'+name+'.json'
        require(protocol.get(name+'_schema') == str(original/relative) and
                protocol.get(name+'_schema_sha256') == files[relative],
                'declaration schema differs: '+name)
        require(type(read(root/relative)) is dict,
                'declaration schema must be an object')
    return root


def access_program_binding(raw, clients, model):
    """Validate the native policy without importing model/CUDA code."""
    import json
    from tools.serve_gateway import client_policy as cp
    require(type(raw) is bytes and 0 < len(raw) <= cp.MAX_BYTES,
            'invalid access program file size')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=cp.unique,
                           parse_constant=lambda _: cp.require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid native access program policy') from None
    require(type(value) is dict and set(value) == {
        'version', 'authority', 'model', 'cyber_tier', 'minimum_grant',
        'default_selection', 'standard_allowed', 'program_instructions', 'clients'},
        'invalid access program fields')
    require(type(value['version']) is int and value['version'] == 1 and
            value['authority'] == 'q4t-local' and value['model'] == model,
            'invalid access program authority or model')
    tier, grant = value['cyber_tier'], value['minimum_grant']
    require(tier in ('blue', 'red') and grant in ('blue', 'red') and
            (tier != 'red' or grant == 'red'), 'invalid native model tier')
    require(value['default_selection'] in ('standard', 'model_tier') and
            type(value['standard_allowed']) is bool and
            (value['default_selection'] != 'standard' or value['standard_allowed']),
            'invalid native program default')
    instructions = value['program_instructions']
    require(type(instructions) is str and instructions.strip(' \t\n\r\v\f') and
            len(instructions.encode('utf-8')) <= 16384 and '\0' not in instructions,
            'invalid native program instructions')
    cp.validate(clients)
    grants = value['clients']
    require(type(grants) is list and len(grants) <= cp.MAX_BYTES // 1024,
            'invalid native program grants')
    known, seen = {v['id'] for v in clients['clients']}, set()
    for entry in grants:
        require(type(entry) is dict and set(entry) == {'id', 'grant'} and
                type(entry['id']) is str and entry['id'] in known and
                entry['id'] not in seen and entry['grant'] in ('blue', 'red'),
                'invalid native client grant')
        seen.add(entry['id'])
    return dict(profile='q4t-access-program-policy-v1', authority='q4t-local',
                model=model, sha256=hashlib.sha256(raw).hexdigest())


def _native_policy_binding(binary, path, raw, command, profile, label,
                           digest_field='sha256'):
    """The hash-verified engine owns policy parsing; this never loads a model."""
    from tools.serve_gateway import client_policy as cp
    require(type(raw) is bytes and 0 < len(raw) <= cp.MAX_BYTES,
            'invalid '+label+' size')
    environment = {k: v for k, v in os.environ.items()
                   if k not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')}
    try:
        result = subprocess.run([str(binary), command, str(path)],
            capture_output=True, timeout=10, check=False, env=environment)
        require(result.returncode == 0 and len(result.stdout) <= 16384,
                'native '+label+' validation failed')
        value = json.loads(result.stdout.decode('utf-8'), object_pairs_hook=cp.unique,
                           parse_constant=lambda _: cp.require(False))
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError) as error:
        raise RuntimeError('native '+label+' validation failed') from error
    require(type(value) is dict and value.get('profile') == profile
            and value.get('configured') is True
            and value.get(digest_field) == hashlib.sha256(raw).hexdigest()
            and value.get('request_override') is False,
            'native '+label+' identity differs')
    return value


def validate_live_transport(value):
    require(type(value) is dict and value.get('profile') == 'q4t-live-transport-v1'
            and value.keys() in ({'profile', 'webrtc_address'},
                                 {'profile', 'webrtc_address', 'sip_trunks_file', 'sip_trunks_sha256'}),
            'invalid Live transport receipt')
    ip = value['webrtc_address']
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError as error:
        raise RuntimeError('invalid Live WebRTC address') from error
    require(type(ip) is str and str(address) == ip and not address.is_multicast
            and int(address) not in (0, 0xffffffff), 'invalid Live WebRTC address')
    if 'sip_trunks_file' in value:
        path, digest = value['sip_trunks_file'], value['sip_trunks_sha256']
        require(type(path) is str and path.startswith('/') and '\0' not in path
                and os.path.normpath(path) == path and len(os.fsencode(path)) <= 4096
                and type(digest) is str and len(digest) == 64
                and all(c in '0123456789abcdef' for c in digest),
                'invalid SIP trunk binding')


def live_transport_binding(binary, address, trunks):
    if address is None and trunks is None:
        return None
    # A sealed package pins the external private file. An absent or changed
    # file degrades only SIP when the engine starts or recovers.
    value = dict(profile='q4t-live-transport-v1',
                 webrtc_address='127.0.0.1' if address is None else address)
    validate_live_transport(value)
    args = [str(binary), 'live-transport-check', value['webrtc_address']]
    digest = ''
    if trunks is not None:
        from tools.serve_gateway import client_policy as cp
        path = Path(trunks)
        path = path.parent.resolve(strict=True) / path.name
        digest = hashlib.sha256(cp.read_private(path)).hexdigest()
        value.update(sip_trunks_file=str(path), sip_trunks_sha256=digest)
        validate_live_transport(value)
        args.append(str(path))
    environment = {k: v for k, v in os.environ.items()
                   if k not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')}
    check = subprocess.run(args, capture_output=True, timeout=10, env=environment)
    require(check.returncode == 0 and len(check.stdout) <= 1024 and
            json.loads(check.stdout) == dict(profile=value['profile'],
                webrtc_address=value['webrtc_address'], sip_trunks_sha256=digest),
            'accepted engine failed native Live transport validation')
    return value


def file_input_policy_binding(binary, path, raw):
    return _native_policy_binding(binary, path, raw, 'file-input-policy-check',
        'q4t-file-input-policy-v1', 'file input policy')


def mcp_policy_binding(binary, path, raw):
    return _native_policy_binding(binary, path, raw, 'mcp-policy-check',
        'q4t-mcp-deployment-policy-v1', 'MCP deployment policy')


def web_policy_binding(binary, path, raw):
    return _native_policy_binding(binary, path, raw, 'web-policy-check',
        'q4t-web-deployment-policy-v1', 'web search deployment policy',
        digest_field='source_sha256')


def prepare(binary, release, model, output, port=8080, restart_limit=0,
            critical_memory_mib=1024, client_policy=None, host_resources=None,
            operations_port=0, nvml_policy='strict', model_names=None,
            application_state=None, capacity_config=None, repository=None,
            access_program_policy=None, file_input_policy=None,
            embedding_model_dir=None, mcp_policy=None, web_policy=None,
            asr_model_dir=None, asr_limits=None, asr_auxiliary=None,
            tts_model_dir=None, tts_limits=None, tts_base_model_dir=None,
            sip_trunks_file=None, live_webrtc_address=None):
    check_python_runtime()
    repository = ROOT if repository is None else Path(repository).resolve(strict=True)
    profile = None
    if capacity_config is not None:
        from tools.serve_service import capacity
        profile = capacity.normalize(capacity_config)
    require(tts_model_dir is not None or tts_base_model_dir is not None or
            tts_limits is None,
            'native TTS limits require a TTS model directory')
    require(tts_limits is None or type(tts_limits) is dict and
            tts_limits.keys() <= TTS_LIMITS.keys(), 'invalid native TTS overrides')
    require(asr_model_dir is not None or asr_limits is None,
            'native ASR limits require an ASR model directory')
    require(asr_limits is None or type(asr_limits) is dict and
            asr_limits.keys() <= ASR_LIMITS.keys(), 'invalid native ASR overrides')
    auxiliary = {} if asr_auxiliary is None else asr_auxiliary_settings(
        asr_auxiliary, voice_registration=tts_base_model_dir is not None)
    require(not auxiliary or asr_model_dir is not None,
            'optional audio backends require an ASR model directory')
    config = settings(port, restart_limit, critical_memory_mib, operations_port,
                      nvml_policy, model_names, capacity_profile=profile,
                      native_embedding=(dict(NATIVE_EMBEDDING,
                          directory=str(embedding_model_dir))
                          if embedding_model_dir is not None else None),
                      native_asr=(dict(NATIVE_ASR, directory=str(asr_model_dir),
                          limits=dict(ASR_LIMITS, **(asr_limits or {})), **auxiliary)
                          if asr_model_dir is not None else None),
                      native_tts=(dict(NATIVE_TTS, directory=str(tts_model_dir),
                          limits=dict(TTS_LIMITS, **(tts_limits or {})))
                          if tts_model_dir is not None else None),
                      native_tts_base=(dict(NATIVE_TTS_BASE,
                          directory=str(tts_base_model_dir),
                          limits=dict(TTS_LIMITS, **(tts_limits or {})))
                          if tts_base_model_dir is not None else None))
    policy_raw = None
    if host_resources is not None:
        from tools.serve_service import resources
        resources.validate(host_resources)
    if client_policy is not None:
        from tools.serve_gateway import client_policy as cp
        policy_raw, clients = cp.read(client_policy)
        if profile is not None:
            capacity.check_clients(profile, clients)
    access_raw, access_binding = None, None
    if access_program_policy is not None:
        require(policy_raw is not None, 'access programs require client policy')
        access_raw = cp.read_private(access_program_policy)
        access_binding = access_program_binding(access_raw, clients,
            config.get('model_names', {}).get('name', 'qwen3.8-flash-next'))
    file_raw, file_binding = None, None
    if file_input_policy is not None:
        from tools.serve_gateway import client_policy as cp
        file_raw = cp.read_private(file_input_policy)
    mcp_raw, mcp_binding = None, None
    if mcp_policy is not None:
        from tools.serve_gateway import client_policy as cp
        mcp_raw = cp.read_private(mcp_policy)
    web_raw, web_binding = None, None
    if web_policy is not None:
        from tools.serve_gateway import client_policy as cp
        web_raw = cp.read_private(web_policy)
    binary, release, model = map(lambda p: Path(p).resolve(),
                                 (binary, release, model))
    regular(binary)
    metadata = read(release)
    require(sha(binary) == metadata['binary_sha256'], 'binary/sidecar mismatch')
    cache = Path(metadata['build_cache'])
    regular(cache)
    require(sha(cache) == metadata['build_cache_sha256'], 'cache mismatch')
    acceptance = Path(metadata['acceptance_decision'])
    regular(acceptance)
    require(sha(acceptance) == metadata['acceptance_decision_sha256'],
            'acceptance receipt mismatch')
    accepted_release(metadata, read(acceptance))
    protocol = metadata['chat_protocol']['protocol_sha256']
    require(isinstance(protocol, str) and len(protocol) == 64 and
            all(c in '0123456789abcdef' for c in protocol), 'invalid protocol')
    declarations = verify_declarations(metadata, required=True)
    live = live_transport_binding(binary, live_webrtc_address, sip_trunks_file)
    check_media_declaration(metadata, config['profile'])
    if 'native_embedding' in config:
        check_embedding_declaration(metadata)
    if 'native_asr' in config:
        check_asr_declaration(metadata, config['native_asr'],
                              voice_registration='native_tts_base' in config)
    check_tts_declaration(metadata, config.get('native_tts'),
                          config.get('native_tts_base'), config.get('native_asr'))
    if access_raw is not None:
        require(read(declarations/'capabilities.json').get('responses', {})
                .get('access_programs', {}).get('enabled') is True,
                'accepted engine lacks native access programs')
    if file_raw is not None:
        require(read(declarations/'capabilities.json').get('file_inputs', {})
                .get('policy_profile') == 'q4t-file-input-policy-v1',
                'accepted engine lacks native file input policy')
        file_binding = file_input_policy_binding(binary, file_input_policy, file_raw)
    if mcp_raw is not None:
        require(read(declarations/'capabilities.json').get('mcp_deployment', {})
                .get('policy_profile') == 'q4t-mcp-deployment-policy-v1',
                'accepted engine lacks native MCP deployment policy')
        mcp_binding = mcp_policy_binding(binary, mcp_policy, mcp_raw)
    if web_raw is not None:
        require(read(declarations/'capabilities.json').get('web_search', {})
                .get('policy_profile') == 'q4t-web-deployment-policy-v1',
                'accepted engine lacks native web search deployment policy')
        web_binding = web_policy_binding(binary, web_policy, web_raw)
    observed_state = metadata.get('observed_configuration', {}).get('application_state')
    if observed_state is not None:
        require(type(observed_state) is dict and
            observed_state.get('profile') == 'q4t-objects-v1',
            'unknown observed application state profile')
        require(application_state is None or type(application_state) is dict and
            set(application_state) <= {'directory', 'live_recordings', *APPLICATION_LIMITS},
            'unknown application state option')
        if (application_state or {}).get('live_recordings'):
            require(type(observed_state.get('live_recordings')) is bool,
                    'accepted engine does not declare Live recording support')
        config['application_state'] = application_settings(dict(
            profile='q4t-objects-v1', directory='',
            **{k: observed_state.get(k) for k in APPLICATION_LIMITS},
        ) | (application_state or {}))
    else:
        require(application_state is None,
                'accepted engine does not declare native application state')
    model_files = {p.name: sha(p) for p in sorted(model.glob('*.json'))
                   if p.is_file() and not p.is_symlink()}
    require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
            model_files.keys(), 'model metadata incomplete')
    target = private_directory(output, repository)
    for source, name in zip((binary, release, cache, acceptance), FILES):
        shutil.copyfile(source, target/name)
        os.chmod(target/name, 0o500 if name == 'q4t' else 0o400)
    sources = V5_SOURCES if host_resources is not None or operations_port else (V4_SOURCES if policy_raw is not None else SOURCES)
    if nvml_policy == 'degrade':
        sources = V10_SOURCES if host_resources is not None else V9_SOURCES
    if profile is not None:
        sources = V11_SOURCES
    for name in sources:
        source, dest = ROOT/name, target/'source'/name
        regular(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        os.chmod(dest, 0o400)
    names = [*FILES, *('source/'+s for s in sources)]
    for name in NOTICE_FILES:
        source, dest = NOTICE_ROOT/name, target/'licenses'/name
        regular(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        dest.chmod(0o400)
        names.append('licenses/'+name)
    for name in declaration_files(metadata['release_declarations']['profile']):
        dest = target/'declarations'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(declarations/name, dest)
        dest.chmod(0o400)
        names.append('declarations/'+name)
    if policy_raw is not None:
        with (target/'client-policy.json').open('xb') as stream:
            stream.write(policy_raw)
        (target/'client-policy.json').chmod(0o400)
        names.append('client-policy.json')
    if access_raw is not None:
        with (target/'access-program-policy.json').open('xb') as stream:
            stream.write(access_raw)
        (target/'access-program-policy.json').chmod(0o400)
        names.append('access-program-policy.json')
    if file_raw is not None:
        with (target/'file-input-policy.json').open('xb') as stream:
            stream.write(file_raw)
        (target/'file-input-policy.json').chmod(0o400)
        names.append('file-input-policy.json')
    if mcp_raw is not None:
        with (target/'mcp-policy.json').open('xb') as stream:
            stream.write(mcp_raw)
        (target/'mcp-policy.json').chmod(0o400)
        names.append('mcp-policy.json')
    if web_raw is not None:
        with (target/'web-policy.json').open('xb') as stream:
            stream.write(web_raw)
        (target/'web-policy.json').chmod(0o400)
        names.append('web-policy.json')
    python = Path(sys.executable).resolve()
    manifest = dict(version=3, repository=str(repository), model_dir=str(model),
        model_files=model_files, protocol_sha256=protocol, settings=config,
        python=dict(path=str(python), sha256=sha(python)),
        files={name: sha(target/name) for name in names},
        weight_shards_rehashed=False, managed_service_accepted=False)
    if live is not None:
        manifest['live_transport'] = live
    if access_binding is not None:
        manifest['access_program_policy'] = access_binding
    if file_binding is not None:
        manifest['file_input_policy'] = file_binding
    if mcp_binding is not None:
        manifest['mcp_policy'] = mcp_binding
    if web_binding is not None:
        manifest['web_policy'] = web_binding
    manifest['release_declarations'] = metadata['release_declarations']['profile']
    manifest['third_party_notices'] = dict(profile='q4t-notices-v1', files=list(NOTICE_FILES))
    if policy_raw is not None:
        manifest['version'] = 4
        manifest['client_policy'] = cp.binding(policy_raw)
    if host_resources is not None:
        manifest['version'] = 5
        manifest['host_resources'] = dict(host_resources)
        manifest['resource_dependencies'] = resources.dependencies()
    if operations_port:
        manifest['version'] = 6
    if nvml_policy == 'degrade':
        manifest['version'] = 10 if host_resources is not None else 9
    if profile is not None:
        manifest['version'] = 11
    save(target/'manifest.json', manifest)
    verify(target, sha(target/'manifest.json'))
    return dict(package=str(target), manifest_sha256=sha(target/'manifest.json'),
                binary_sha256=manifest['files']['q4t'])


def verify(package, digest):
    package = Path(package).resolve(strict=True)
    require(sha(package/'manifest.json') == digest, 'package manifest changed')
    manifest = read(package/'manifest.json')
    require(type(manifest['version']) is int and manifest['version'] in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
            'unsupported package version')
    if 'live_transport' in manifest:
        validate_live_transport(manifest['live_transport'])
    config = manifest['settings']
    wanted = settings(config['port'], config['restart_limit'],
                      config.get('critical_memory_mib', 1024),
                      config.get('operations_port', 0) if manifest['version'] in (6, 7, 8, 9, 10, 11) else 0,
                      config.get('nvml_policy', 'strict') if manifest['version'] in (7, 8, 9, 10, 11) else 'strict',
                      config.get('model_names'), config.get('application_state'),
                      config['profile'] if manifest['version'] == 11 else None,
                      config.get('native_embedding'), config.get('native_asr'),
                      config.get('native_tts'), config.get('native_tts_base'))
    require(manifest['version'] not in (7, 8, 9, 10) or config.get('nvml_policy') == 'degrade',
            'monitor package requires explicit degradation policy')
    require(manifest['version'] != 10 or 'host_resources' in manifest,
            'resource-control package requires host resources')
    require(manifest['version'] != 6 or config.get('operations_port', 0) != 0,
            'operations package requires explicit port')
    if manifest['version'] == 1:
        for key in ('critical_memory_mib', 'memory_poll_seconds',
                    'memory_low_seconds', 'memory_unknown_seconds'):
            del wanted[key]
    require(same_json(config, wanted), 'unknown managed configuration')
    sources = {1: LEGACY_SOURCES, 2: V2_SOURCES, 3: SOURCES,
               4: V4_SOURCES, 5: V5_SOURCES, 6: V5_SOURCES,
               7: V7_SOURCES, 8: V8_SOURCES, 9: V9_SOURCES,
               10: V10_SOURCES, 11: V11_SOURCES}[manifest['version']]
    expected = {*FILES, *('source/'+name for name in sources)}
    if 'third_party_notices' in manifest:
        notices = manifest['third_party_notices']
        require(type(notices) is dict and notices.keys() == {'profile', 'files'} and
            notices['profile'] == 'q4t-notices-v1' and type(notices['files']) is list and
            len(notices['files']) <= 256 and
            all(type(n) is str and n and '\0' not in n and not n.startswith('/') and
                not {'', '.', '..'}.intersection(n.split('/')) for n in notices['files']) and
            len(set(notices['files'])) == len(notices['files']),
            'invalid third-party notice inventory')
        require({'openai-node/LICENSE', 'openai-node/README.md',
                 'structured_output/XGrammar-LICENSE',
                 'structured_output/regex-syntax-LICENSE-APACHE'} <= set(notices['files']),
                'required third-party notices absent')
        expected.update('licenses/'+n for n in notices['files'])
    if 'release_declarations' in manifest:
        expected.update('declarations/'+n for n in
                        declaration_files(manifest['release_declarations']))
    if manifest['version'] == 4 or manifest['version'] in (5, 6, 7, 8, 9, 10, 11) and 'client_policy' in manifest:
        from tools.serve_gateway import client_policy as cp
        expected.add('client-policy.json')
        raw, clients = cp.read(package/'client-policy.json')
        if manifest['version'] == 11:
            from tools.serve_service import capacity
            capacity.check_clients(config['profile'], clients)
        require(manifest.get('client_policy') == cp.binding(raw),
                'client policy binding differs')
    else:
        require('client_policy' not in manifest, 'legacy package cannot scope clients')
    if manifest['version'] == 5 or manifest['version'] in (6, 7, 8, 9, 10, 11) and 'host_resources' in manifest:
        from tools.serve_service import resources
        resources.validate(manifest['host_resources'])
        resources.verify_dependencies(manifest['resource_dependencies'])
    else:
        require('host_resources' not in manifest and 'resource_dependencies' not in manifest,
                'legacy package cannot declare host resource protection')
    if 'access_program_policy' in manifest:
        require('client_policy' in manifest,
                'access programs require sealed client policy')
        expected.add('access-program-policy.json')
        access_raw = cp.read_private(package/'access-program-policy.json')
        require(manifest['access_program_policy'] == access_program_binding(
            access_raw, clients, config.get('model_names', {}).get(
                'name', 'qwen3.8-flash-next')), 'access program binding differs')
    if 'file_input_policy' in manifest:
        expected.add('file-input-policy.json')
    if 'mcp_policy' in manifest:
        expected.add('mcp-policy.json')
    if 'web_policy' in manifest:
        expected.add('web-policy.json')
    require(set(manifest['files']) == expected, 'package inventory differs')
    for name, wanted in manifest['files'].items():
        path = package/name
        regular(path)
        require(path.resolve().is_relative_to(package), 'package path escaped')
        require(sha(path) == wanted, 'package file changed: '+name)
    require(sha(manifest['python']['path']) == manifest['python']['sha256'],
            'Python interpreter changed')
    metadata = read(package/'q4t.release.json')
    require(('release_declarations' in metadata) ==
            ('release_declarations' in manifest),
            'package declaration profile differs')
    if 'release_declarations' in manifest:
        require(manifest['release_declarations'] == metadata['release_declarations']['profile'],
                'package declaration profile differs')
    verify_declarations(metadata, package/'declarations',
                        required='release_declarations' in manifest)
    check_media_declaration(metadata, config['profile'])
    program = metadata.get('chat_protocol', {}).get('responses', {}).get(
        'programmatic_tools', {})
    if program.get('profile') == 'q4t-programmatic-tools-v1':
        require({'quickjs/LICENSE', 'quickjs/SOURCE.json'} <=
                set(manifest.get('third_party_notices', {}).get('files', [])),
                'program runtime notices absent')
    if 'access_program_policy' in manifest:
        require(read(package/'declarations/capabilities.json').get('responses', {})
                .get('access_programs', {}).get('enabled') is True,
                'engine access program declaration absent')
    if 'file_input_policy' in manifest:
        from tools.serve_gateway import client_policy as cp
        require(read(package/'declarations/capabilities.json').get('file_inputs', {})
                .get('policy_profile') == 'q4t-file-input-policy-v1',
                'engine file input policy declaration absent')
        file_path = package/'file-input-policy.json'
        require(manifest['file_input_policy'] == file_input_policy_binding(
            package/'q4t', file_path, cp.read_private(file_path)),
            'file input policy binding differs')
    if 'mcp_policy' in manifest:
        from tools.serve_gateway import client_policy as cp
        require(read(package/'declarations/capabilities.json').get('mcp_deployment', {})
                .get('policy_profile') == 'q4t-mcp-deployment-policy-v1',
                'engine MCP deployment policy declaration absent')
        mcp_path = package/'mcp-policy.json'
        require(manifest['mcp_policy'] == mcp_policy_binding(
            package/'q4t', mcp_path, cp.read_private(mcp_path)),
            'MCP deployment policy binding differs')
    if 'web_policy' in manifest:
        from tools.serve_gateway import client_policy as cp
        require(read(package/'declarations/capabilities.json').get('web_search', {})
                .get('policy_profile') == 'q4t-web-deployment-policy-v1',
                'engine web search deployment policy declaration absent')
        web_path = package/'web-policy.json'
        require(manifest['web_policy'] == web_policy_binding(
            package/'q4t', web_path, cp.read_private(web_path)),
            'web search deployment policy binding differs')
    if 'application_state' in config:
        require(metadata.get('observed_configuration', {}).get('application_state', {})
            .get('profile') == 'q4t-objects-v1', 'application state engine declaration absent')
        if config['application_state'].get('live_recordings'):
            require(type(metadata['observed_configuration']['application_state'].get('live_recordings')) is bool,
                    'accepted engine does not declare Live recording support')
    if 'native_embedding' in config:
        check_embedding_declaration(metadata)
    if 'native_asr' in config:
        check_asr_declaration(metadata, config['native_asr'],
                              voice_registration='native_tts_base' in config)
    check_tts_declaration(metadata, config.get('native_tts'),
                          config.get('native_tts_base'), config.get('native_asr'))
    accepted_release(metadata, read(package/'acceptance.json'))
    require(metadata['binary_sha256'] == manifest['files']['q4t'] and
            metadata['build_cache_sha256'] == manifest['files']['CMakeCache.txt']
            and metadata['acceptance_decision_sha256'] ==
            manifest['files']['acceptance.json'] and
            metadata['chat_protocol']['protocol_sha256'] ==
            manifest['protocol_sha256'], 'inconsistent package identities')
    model = Path(manifest['model_dir'])
    for name, wanted in manifest['model_files'].items():
        require(Path(name).name == name and name.endswith('.json'),
                'invalid model metadata path')
        regular(model/name)
        require(sha(model/name) == wanted, 'model metadata changed: '+name)
    require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
            manifest['model_files'].keys(), 'model metadata incomplete')
    return manifest


def check_embedding_declaration(metadata):
    capability = metadata.get('chat_protocol', {}).get('embeddings', {}).get('native_backend', {})
    observed = metadata.get('observed_configuration', {}).get('native_embedding', {})
    require(capability.get('available') is True and observed.get('enabled') is True and
            all(capability.get(k) == v and observed.get(k) == v
                for k, v in NATIVE_EMBEDDING.items()),
            'accepted engine does not declare configured native embedding backend')


def check_asr_declaration(metadata, selected, *, voice_registration=False):
    selected = asr_settings(selected, voice_registration=voice_registration)
    capability = metadata.get('chat_protocol', {}).get('transcriptions', {})
    configuration = metadata.get('observed_configuration', {})
    health = configuration.get('native_asr_health', {})
    require(capability.get('configured') is True and
            all(capability.get(k) == v for k, v in NATIVE_ASR.items()) and
            health.get('model') == NATIVE_ASR['model'] and
            health.get('core_readiness_dependency') is False and
            health.get('state') in ('configured', 'loading', 'metadata_ready',
                                    'ready', 'degraded') and
            same_json(configuration.get('native_asr_limits'), selected['limits']),
            'accepted engine does not declare configured native ASR limits/identity')
    for name, (fixed, _, _) in ASR_AUXILIARY.items():
        declared = capability.get(name, {})
        if name not in selected:
            require(declared.get('configured') is not True,
                    'package drops configured optional audio backend: '+name)
            continue
        health = configuration.get('native_'+name+'_health', {})
        actual_limits = asr_auxiliary_observed_limits(configuration, name)
        require(declared.get('configured') is True and
                all(declared.get(k) == v for k,v in fixed.items()) and
                health.get('model') == fixed['model'] and
                health.get('core_readiness_dependency') is False and
                health.get('state') in ('configured', 'loading', 'metadata_ready',
                                        'ready', 'degraded') and
                same_json(actual_limits, selected[name]['limits']),
                'accepted engine does not declare optional audio limits/identity: '+name)


def check_tts_capabilities(capabilities, selected, base=None, asr=None):
    speech = capabilities.get('speech', {})
    chat = capabilities.get('chat_audio_output', {})
    registration = capabilities.get('voice_registration', {})
    require(all(type(v) is dict for v in (speech, chat, registration)),
            'invalid native TTS capabilities')
    if 'registered_voice_conditioning' in speech:
        require(same_json(speech['registered_voice_conditioning'], VOICE_CONDITIONING),
                'registered voice conditioning contract differs')
    enabled = selected is not None or base is not None
    if not enabled:
        require(speech.get('enabled') is not True and chat.get('enabled') is not True,
                'package drops configured native TTS backend')
    else:
        fixed = NATIVE_TTS if selected is not None else NATIVE_TTS_BASE
        require(speech.get('enabled') is True and chat.get('enabled') is True and
                all(speech.get(k) == v for k, v in fixed.items()) and
                speech.get('core_health_dependency') is False,
                'accepted engine does not declare configured native TTS identity')
    require(speech.get('registered_custom_voices', False) is (base is not None) and
            speech.get('builtin_enabled', selected is not None) is (selected is not None),
            'package drops or changes a configured native TTS family')
    require(asr is None or type(asr) is dict, 'invalid native voice ASR binding')
    expected_registration = (base is not None and asr is not None and
                             {'speaker', 'vad'} <= asr.keys())
    require(registration.get('enabled', False) is expected_registration,
            'package changes configured voice registration dependencies')
    if base is not None:
        require(speech.get('registered_voice_model') == NATIVE_TTS_BASE['model'] and
                speech.get('base_revision') == NATIVE_TTS_BASE['revision'] and
                speech.get('base_weight_sha256') == NATIVE_TTS_BASE['weight_sha256'],
                'accepted engine does not declare configured TTS Base identity')
        require(registration.get('owner_scoped') is True and
                registration.get('recheck_consent_per_synthesis') is True and
                registration.get('metadata_requires_healthy_model') is False and
                registration.get('core_health_dependency') is False,
                'voice registration owner/revocation/isolation contract differs')


def check_tts_declaration(metadata, selected, base=None, asr=None):
    check_tts_capabilities(metadata.get('chat_protocol', {}), selected, base, asr)
    configuration = metadata.get('observed_configuration', {})
    for key, value, is_base in (('native_tts', selected, False),
                                ('native_tts_base', base, True)):
        health = configuration.get(key+'_health', {})
        require(type(health) is dict, 'invalid native TTS health')
        if value is None:
            require(health.get('state', 'disabled') == 'disabled',
                    'package drops configured native TTS backend: '+key)
            continue
        value = tts_settings(value, base=is_base)
        require(health.get('model') == value['model'] and
                health.get('core_readiness_dependency') is False and
                health.get('state') in ('configured', 'loading', 'metadata_ready',
                                        'ready', 'degraded') and
                same_json(configuration.get('native_tts_limits'), value['limits']),
                'accepted engine does not declare configured native TTS limits/identity')
    voice = configuration.get('native_voice_health', {})
    require(type(voice) is dict, 'invalid native voice health')
    if base is None:
        require(voice.get('state', 'disabled') == 'disabled',
                'package drops configured native voice condition backend')
    else:
        icl = 'registered_voice_conditioning' in metadata['chat_protocol']['speech']
        # Old accepted engines retain their speaker-only declaration. Current
        # ICL engines must bind the reference encoder; neither may impersonate
        # the other by only changing a model name or dropping the capability.
        require(voice.get('model') == NATIVE_TTS_BASE['model']+
                    ('-reference' if icl else '-speaker') and
                voice.get('reference_encoder', False) is icl and
                voice.get('core_readiness_dependency') is False and
                voice.get('state') in ('configured', 'loading', 'metadata_ready',
                                       'ready', 'degraded'),
                'accepted engine does not declare isolated native voice condition')


def check_media_declaration(metadata, profile):
    """Opt-in media requires an actually accepted media-enabled engine."""
    if profile.get('allow_media', False):
        caps = metadata.get('chat_protocol', {}).get('runtime', {})
        observed = metadata.get('observed_configuration', {}).get('runtime', {})
        require(caps.get('media_enabled') is True and
                observed.get('media_enabled') is True,
                'accepted engine does not declare enabled media')


def live_transport_args(manifest):
    args = []
    if 'live_transport' in manifest:
        live = manifest['live_transport']
        args += ['--live-webrtc-address', live['webrtc_address']]
        if 'sip_trunks_file' in live:
            args += ['--sip-trunks-file', live['sip_trunks_file'],
                     '--sip-trunks-sha256', live['sip_trunks_sha256']]
    return args


def command(package, manifest, *, state_directory=None):
    cfg = manifest['settings']
    args = [str(Path(package)/'q4t'), 'serve', '--model-dir', manifest['model_dir'],
            '--host', '127.0.0.1', '--port', str(cfg['port']),
            '--mtp' if cfg['profile'].get('mtp_enabled', True) else '--no-mtp']
    for key, value in cfg['profile'].items():
        if key not in ('name', 'host', 'mtp_enabled', 'allow_media'):
            args += ['--'+key.replace('_', '-'), str(value)]
    if cfg['profile'].get('allow_media', False):
        args += ['--allow-media']
    args += live_transport_args(manifest)
    if 'client_policy' in manifest:
        args += ['--client-policy-file', str(Path(package)/'client-policy.json')]
    if 'access_program_policy' in manifest:
        args += ['--access-program-policy-file',
                 str(Path(package)/'access-program-policy.json')]
    if 'file_input_policy' in manifest:
        args += ['--file-input-policy-file', str(Path(package)/'file-input-policy.json')]
    if 'mcp_policy' in manifest:
        args += ['--mcp-policy-file', str(Path(package)/'mcp-policy.json')]
    if 'web_policy' in manifest:
        args += ['--web-policy-file', str(Path(package)/'web-policy.json')]
    if cfg.get('operations_port', 0):
        args += ['--operations-port', str(cfg['operations_port'])]
    if 'model_names' in cfg:
        args += ['--served-model-name', cfg['model_names']['name']]
        for alias in cfg['model_names']['aliases']:
            args += ['--model-alias', alias]
    if 'native_embedding' in cfg:
        selected = embedding_settings(cfg['native_embedding'])
        args += ['--embedding-model-dir', selected['directory']]
    if 'native_asr' in cfg:
        selected = asr_settings(cfg['native_asr'],
                                voice_registration='native_tts_base' in cfg)
        args += ['--asr-model-dir', selected['directory']]
        for key, value in selected['limits'].items():
            args += ['--asr-'+key.replace('_', '-'), str(value)]
        for name, (_, _, prefix) in ASR_AUXILIARY.items():
            if name in selected:
                args += ['--'+prefix+'-model-dir', selected[name]['directory']]
                for key, value in selected[name]['limits'].items():
                    args += ['--'+prefix+'-'+key.replace('_', '-'), str(value)]
    tts_limits = None
    for name, is_base, flag in (('native_tts', False, '--tts-model-dir'),
                                ('native_tts_base', True, '--tts-base-model-dir')):
        if name not in cfg:
            continue
        selected = tts_settings(cfg[name], base=is_base)
        require(tts_limits is None or same_json(tts_limits, selected['limits']),
                'native TTS families must share effective limits')
        tts_limits = selected['limits']
        args += [flag, selected['directory']]
    for key, value in (tts_limits or {}).items():
        args += ['--tts-'+key.replace('_', '-'), str(value)]
    if 'application_state' in cfg:
        objects = application_settings(cfg['application_state'])
        directory = objects['directory']
        if not directory:
            require(state_directory is not None, 'application state needs a deployment directory')
            directory = str(Path(state_directory)/'application-objects')
        application_settings(dict(objects, directory=directory))
        args += ['--object-store-dir', directory]
        if objects.get('live_recordings'):
            args.append('--live-recordings')
        for key in APPLICATION_LIMITS:
            args += ['--object-'+key.replace('_', '-'), str(objects[key])]
    return args
