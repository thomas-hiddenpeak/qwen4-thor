"""Qualify the same image source and spoken output across the managed capacities."""
import re
import shutil
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'response_media_handoff'
RECEIPT = reuse.MEDIA_HANDOFF_RECEIPT
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-agent-structured-format-20261126/delivery.json'
PROOF = lifecycle.ROOT / 'docs/evidence/serve-image-speech-recovery-20261126/combined.json'
ACTOR = Path(__file__).with_name('media_profile_sdk.mjs')


def prepare(out, build, deployment, image):
    proof = package.read(PROOF)
    identity = {name: package.sha(build / executable)
                for name, executable in (('engine', 'q4t'), ('ingress', 'q4t-ingress'))}
    package.require(identity == proof['identity'] and proof['status'] == 'VERIFIED_RETAINED_NATIVE_TLS_AND_STARTUP_RECOVERY'
                    and proof['geometry_tokens_saved'] == 528 and len(proof['decoded']) == 4
                    and package.sha(image) == proof['image_sha256'],
                    'media qualification needs the tested binary, ingress and original image')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.SESSION_CONTROL_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])
    target = out / 'source.png'
    shutil.copyfile(image, target)
    package.require(package.sha(target) == proof['image_sha256'], 'image copy changed')
    for name in ('primary', 'light'):
        path = out / name / 'plan.json'
        plan = package.read(path)
        plan[KEY].update(image_sha256=proof['image_sha256'])
        package.save(path, plan)


def sdk_parameters(stage):
    image = stage.out.parent / 'source.png'
    expected = stage.plan[KEY]['image_sha256']
    package.require(package.sha(image) == expected, 'frozen profile image changed')
    return dict(image=str(image), sha256=expected, voice_model=stage.plan['native_tts']['model'])


def check(row, previous=None):
    primary = previous is None
    source, current = row['source'], row['current']
    image = package.read(PROOF)['image_sha256']
    for item in (source, current):
        package.require(isinstance(item['id'], str) and re.fullmatch(r'resp_[0-9a-f]{32}', item['id'])
                        and item['color'] == 'blue' and item['source_sha256'] == image
                        and type(item['tokens']) is int and item['tokens'] > 0
                        and re.fullmatch(r'[0-9a-f]{64}', item['audio_sha256'])
                        and type(item['audio_bytes']) is int and 64 < item['audio_bytes'] <= 2**21,
                        'invalid image or audio evidence')
    package.require(row['sdk'] == '7.47.0' and row['peer'] == 404
                    and row['phase'] == ('primary' if primary else 'light')
                    and source['policy'] == 'disabled' and source['mime'] == 'audio/opus'
                    and re.fullmatch(r'file_[0-9a-f]{32}', source['file']),
                    'invalid media handoff source or owner separation')
    if primary:
        package.require(source == dict(current, file=source['file']), 'primary result differs from saved source')
    else:
        check(previous)
        package.require(source == previous['source'] and current['id'] != source['id']
                        and current['policy'] == 'enabled' and current['mime'] == 'application/octet-stream'
                        and source['tokens'] - current['tokens'] == 528,
                        'media handoff changed the source or lost the measured geometry')
