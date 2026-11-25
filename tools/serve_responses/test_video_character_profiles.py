"""Check video reference qualification using the original recorded SDK resource."""
import copy
import unittest

from tools.serve_recovery import profile_live_reuse as reuse, profile_migration
from tools.serve_responses import video_character_profiles as profiles
from tools.serve_service import package


class VideoCharacterProfiles(unittest.TestCase):
    def test_extension_keeps_existing_capabilities_and_exact_upload_bounds(self):
        before = dict(chatkit=dict(input_transcribe=dict(operation='input.transcribe', configured=True)))
        target = dict(before, video_characters=reuse.CHARACTER_CAPABILITY)
        self.assertTrue(profile_migration.explicit_extension(before, target))
        self.assertFalse(profile_migration.explicit_extension(target, before))
        for key in reuse.CHARACTER_CAPABILITY:
            altered = copy.deepcopy(target)
            del altered['video_characters'][key]
            self.assertFalse(profile_migration.explicit_extension(before, altered), key)
        self.assertFalse(profile_migration.explicit_extension(
            before, dict(target, chatkit={})))

    def test_retrieval_handoff_requires_original_resource_and_fresh_tool_lookup(self):
        proof = package.read(profiles.PROOF)
        row = dict(proof['sdk'])
        full = dict(status=200, value=row['resource'])
        row['wire'] = [dict(full, method='post', path='/videos/characters'),
                       dict(full, method='get', path='/videos/characters/{character_id}')]
        profiles.check_result(row, proof)
        after = dict(row, wire=row['wire'][1:])
        profiles.check_result(after, proof, row)
        for field, wrong in [('wire', row['wire']), ('foreign', 200),
                             ('clip_sha256', '0'*64), ('lookup', {})]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                profiles.check_result(dict(after, **{field: wrong}), proof, row)
        other = copy.deepcopy(row)
        other['resource']['created_at'] += 1
        with self.assertRaises(RuntimeError):
            profiles.check_result(after, proof, other)
