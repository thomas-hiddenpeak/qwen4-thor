"""Qualify consumption of saved voices without registration-only backends.

The saved state and complete audio witnesses come from an accepted real SDK
registration. Mutable state is copied for each profile; the original stays
unchanged. This helper cannot register voices or start another backend.
"""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import wave

from tools.serve_recovery import ledger, profile_evidence as evidence
from tools.serve_recovery import profile_voice
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service import capacity
from tools.serve_service.package import read, require, save, sha

POSTS_PER_PROFILE = 8
MAX_STATE_BYTES = 16 << 20


def inputs(path):
    """Bind the original receipt, every saved byte, owner and TTS condition."""
    path = Path(path).resolve(strict=True)
    witness = evidence.VOICE_CONSUMER_WITNESS
    require(sha(path) == witness['state_sha256'],
            'consumer saved-state receipt differs')
    value = read(path)
    ledger.keys(value, ('files', 'consent', 'voice', 'completion',
                        'isolated_fixture', 'use'))
    require(value['isolated_fixture'] is True and
        all(value[key] == witness[key] for key in ('consent', 'voice')),
        'consumer saved IDs differ')
    directory = path.parent/'registered-state'
    require(directory.is_dir() and not directory.is_symlink(),
            'consumer saved-state directory invalid')
    files = value['files']
    require(type(files) is dict and 1 <= len(files) <= 512 and
        '.q4t-format' in files, 'consumer saved-state inventory invalid')
    require({p.name for p in directory.iterdir()} == set(files),
            'consumer saved-state inventory is incomplete')
    total = 0
    for name, digest in files.items():
        require(type(name) is str and Path(name).name == name and
            name not in ('.', '..') and type(digest) is str and
            ledger.HASH.fullmatch(digest), 'unsafe consumer saved-state entry')
        source = directory/name
        require(source.is_file() and not source.is_symlink(),
                'consumer saved-state entry is not a regular file')
        total += source.stat().st_size
        require(total <= MAX_STATE_BYTES and sha(source) == digest,
                'consumer saved state exceeds bound or changed')
    owner = hashlib.sha256(('client:'+witness['owner']).encode()).hexdigest()
    records = {}
    for key, kind in (('consent', 'voice_consent'), ('voice', 'voice')):
        record = read(directory/(owner+'.'+kind+'.'+witness[key]+'.json'))['record']
        require(record['owner'] == owner and record['id'] == witness[key] and
            record['kind'] == kind and record['expires_at'] == 0,
            'consumer record owner or identity differs')
        records[key] = record
    voice = records['voice']['data']
    require(voice['consent'] == witness['consent'] and
        ledger.digest(voice['condition']) == witness['condition_sha256'],
        'consumer TTS condition or live consent differs')
    metadata = {}
    for key, field in (('consent', 'language'), ('voice', 'type')):
        record = records[key]
        metadata[key] = dict(id=record['id'], created_at=record['created_at'],
            object='audio.'+record['kind'], name=record['data']['name'],
            **{field: record['data'][field]})
    return dict(manifest=str(path), sha256=sha(path), directory=str(directory),
                files=files, metadata=metadata, witness=copy.deepcopy(witness))


def clone(value, destination):
    require(inputs(value['manifest']) == value, 'consumer input drift')
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(),
            'consumer destination must be new and private')
    destination.mkdir(mode=0o700)
    for name, digest in value['files'].items():
        target = destination/name
        shutil.copyfile(Path(value['directory'])/name, target)
        target.chmod(0o600)
        require(sha(target) == digest, 'consumer saved-state copy differs')


def client_policy(value, profile, tts):
    value = copy.deepcopy(value)
    require(len(value['clients']) == 1, 'consumer driver expects one core client')
    core = value['clients'][0]
    require(core['id'] != evidence.VOICE_CONSUMER_WITNESS['owner'],
            'consumer owner must differ from cross-client control')
    core['max_inflight'] = 1
    owner = dict(core, id=evidence.VOICE_CONSUMER_WITNESS['owner'],
        max_output_tokens=max(1536, profile['max_tokens']+tts['limits']['max_frames']*16+1),
        max_body_bytes=profile_voice.MAX_AUDIO_BYTES+65536,
        requests_per_minute=32, tokens_per_minute=200000)
    core['max_output_tokens'] = owner['max_output_tokens']
    value['clients'].append(owner)
    capacity.check_clients(profile, value)
    return value


def unconfigured_upload():
    # Syntactically valid audio tests missing-backend admission only; it is
    # never presented as a valid recording, consent, or acoustic quality case.
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
        audio.writeframes(b'\0\0'*24000)
    return out.getvalue()


class Lifecycle(profile_voice.Lifecycle):
    def __init__(self, stage):
        self.stage = stage
        self.requests = 0
        self.source = stage.plan['voice_consumer']
        require(inputs(self.source['manifest']) == self.source,
                'consumer inputs drifted before business')

    def http(self, *args, **options):
        options.setdefault('client', self.source['witness']['owner'])
        return super().http(*args, **options)

    def run(self):
        from tools.native_tts.http_acceptance import sse
        consent_id = self.source['witness']['consent']
        voice = self.source['metadata']['voice']
        rows = evidence.voice_requests(consent_id, voice['id'])
        rows.pop('transcription')
        consent = self.created(self.http('existing-consent',
            profile_voice.CONSENTS+'/'+consent_id), 'voice_consent')
        require(consent == self.source['metadata']['consent'],
                'consumer consent changed after copying saved state')
        negative = dict(cross_consent=self.rejected(self.http('cross-consent',
            profile_voice.CONSENTS+'/'+consent_id, client=encrypted.CLIENT), 404))
        for key, path, field in (('new_consent', profile_voice.CONSENTS, 'recording'),
                                  ('new_voice', profile_voice.VOICES, 'audio_sample')):
            negative[key] = self.rejected(self.form(key, path,
                rows['consent' if key == 'new_consent' else 'voice'], field,
                unconfigured_upload()), 503, 'voice_verification_not_configured')
        for key, path in (('speech', '/v1/audio/speech'),
                            ('chat', '/v1/chat/completions')):
            negative['cross_'+key] = self.rejected(self.http('cross-'+key,
                path, rows[key], client=encrypted.CLIENT), 404)
        status, wire = self.http('speech', '/v1/audio/speech', rows['speech'])
        require(status == 200, 'saved voice Speech failed without registration backends')
        audio, usage = sse(wire)
        speech = dict(audio_base64=base64.b64encode(audio).decode(), usage=usage['usage'])
        chat = self.accepted(self.http('chat', '/v1/chat/completions', rows['chat']))
        stored = self.accepted(self.http('stored-chat', '/v1/chat/completions/'+chat['id']))
        revoked = dict(consent_deleted=self.accepted(self.http('revoke',
            profile_voice.CONSENTS+'/'+consent_id, method='DELETE')))
        for key, path in (('speech', '/v1/audio/speech'),
                            ('chat', '/v1/chat/completions')):
            revoked[key] = self.rejected(self.http('revoked-'+key, path, rows[key]), 404)
        revoked['consent'] = self.rejected(self.http('revoked-consent',
            profile_voice.CONSENTS+'/'+consent_id), 404)
        revoked['voice_deleted'] = self.accepted(self.http('delete',
            profile_voice.VOICES+'/'+voice['id'], method='DELETE'))
        status, raw = self.stage.http('/v1/config',
            label=self.stage.profile_name+'-consumer-configuration')
        require(status == 200, 'consumer health observation unavailable')
        config = json.loads(raw)
        health = {key: config['native_'+key+'_health'] for key in
                  ('tts', 'asr', 'alignment', 'vad', 'speaker', 'separator')}
        health.update(base=config['native_tts_base_health'], condition=config['native_voice_health'])
        result = dict(requests=rows, consent=consent, voice=voice, speech=speech,
            chat=chat, stored_chat=stored, negative=negative, revocation=revoked,
            health=health, limits=config['native_tts_limits'], witness=self.source['witness'])
        require(self.requests == POSTS_PER_PROFILE, 'consumer request checklist incomplete')
        evidence.voice_consumer_business(result, self.stage.plan['identity'])
        return result
