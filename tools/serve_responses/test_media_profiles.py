"""Check the evidence binding for cross-profile image and speech handoff."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.native_tts import verify_response_media
from tools.serve_recovery import ledger, profile_live_reuse as reuse, profile_record
from tools.serve_responses import media_profiles as media
from tools.serve_service import package


class MediaProfileTests(unittest.TestCase):
    def test_handoff_rejects_replaced_content_geometry_and_owner(self):
        rows = package.read(media.PROOF.with_name('profiles.json'))['profiles']
        before, after = rows['primary']['sdk'], rows['light']['sdk']
        media.check(after, before)
        for key, field, value in [
                ('source', 'file', 'file_' + '0' * 32),
                ('source', 'audio_sha256', '0' * 64),
                ('source', 'id', after['current']['id']),
                ('current', 'tokens', after['current']['tokens'] + 1),
                ('current', 'tokens', True),
                ('current', 'policy', 'disabled'),
                ('current', 'mime', 'audio/opus'),
                ('current', 'source_sha256', '0' * 64),
                ('current', 'color', 'red')]:
            with self.subTest(key=key, field=field), self.assertRaises(RuntimeError):
                row = deepcopy(after)
                row[key][field] = value
                media.check(row, before)
        with self.assertRaises(RuntimeError):
            media.check(dict(after, peer=200), before)

    def test_large_media_record_requires_the_media_format(self):
        record = dict(format=reuse.MEDIA_HANDOFF_RECORD,
                      reused_business=dict(format=reuse.MEDIA_HANDOFF_FORMAT))
        profile_record.shape(record, profile_record.LEGACY_BYTES + 1)
        for format_name in (reuse.SESSION_CONTROL_FORMAT, reuse.CHATKIT_WIDGET_FORMAT):
            with self.subTest(source=format_name), self.assertRaises(ledger.LedgerError):
                profile_record.shape(dict(record, reused_business=dict(format=format_name)),
                                     profile_record.LEGACY_BYTES + 1)

    def test_decode_rejects_replaced_bytes_and_incorrect_sdk_media(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            rows = [dict(name=k, accept=v[0], mime=v[1], bytes=4,
                         sha256=hashlib.sha256(b'AAAA').hexdigest())
                    for k, v in verify_response_media.TYPES.items()]
            saved = output / 'sdk-result.json'
            saved.write_text(json.dumps(dict(negative=[406, 400], rows=rows)))
            (output / 'sdk.opus').write_bytes(b'BBBB')
            with self.assertRaisesRegex(ValueError, 'differs from the SDK'):
                verify_response_media.verify(output)
            rows[0]['mime'] = 'audio/ogg'
            saved.write_text(json.dumps(dict(negative=[406, 400], rows=rows)))
            with self.assertRaisesRegex(ValueError, 'media type'):
                verify_response_media.verify(output)
