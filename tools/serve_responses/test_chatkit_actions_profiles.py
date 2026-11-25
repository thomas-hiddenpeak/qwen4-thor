from copy import deepcopy
import unittest

from tools.serve_responses import chatkit_actions_profiles as profiles
from tools.serve_service import package


class ChatKitActionsHandoff(unittest.TestCase):
    def setUp(self):
        combined = package.read(profiles.PROOF)['combined']['sdk']
        self.primary = {key:deepcopy(combined[key]) for key in ('sdk','chat','revision','source')}
        self.primary['phase'] = 'primary'
        self.light = dict(deepcopy(self.primary), phase='light', recovered=deepcopy(combined['recovered']))

    def test_binds_original_widget_form_and_call(self):
        profiles.check_result(self.primary)
        profiles.check_result(self.light, self.primary)
        for field, bad in [('call','call_other'), ('answered_form','fc_other'),
                           ('widget_label','unverified'), ('answer','incorrect'),
                           ('new_turn',200), ('untyped',200), ('duplicate',200),
                           ('foreign',200), ('deleted',200)]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                changed = deepcopy(self.light)
                changed['recovered'][field] = bad
                profiles.check_result(changed, self.primary)

    def test_rejects_reordered_items_and_a_different_deployment(self):
        for fields in [('user','widget'), ('file','workflow')]:
            row = deepcopy(self.light)
            source = row['source']
            source[fields[0]], source[fields[1]] = source[fields[1]], source[fields[0]]
            with self.assertRaises(RuntimeError):
                profiles.check_result(row, self.primary)
        row = deepcopy(self.light)
        row['recovered']['ids'][1:3] = reversed(row['recovered']['ids'][1:3])
        with self.assertRaises(RuntimeError):
            profiles.check_result(row, self.primary)
        with self.assertRaises(RuntimeError):
            profiles.check_result(dict(self.primary, chat='wrong'))


class BrowserOutputHandoff(unittest.TestCase):
    def test_recovery_matches_the_registered_primary_and_visible_state(self):
        rows = package.read(profiles.BROWSER_PROOF.with_name('profiles.json'))['profiles']
        before = rows['primary']['sdk']
        after = rows['light']['sdk']
        profiles.check_browser_result(before)
        profiles.check_browser_result(after, before)
        for key, value in [('removed', before['source']['form']), ('late_error','success'),
                           ('title','Pending 508'), ('answer','999'), ('call','call_unrelated'),
                           ('events',after['recovered']['events'][:-1]), ('ids',[]),
                           ('answered_form','fc_unrelated'), ('missing',200), ('duplicate',200)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                changed['recovered'][key] = value
                profiles.check_browser_result(changed, before)


class GenerationHandoff(unittest.TestCase):
    def test_original_partial_document_and_isolation_survive_the_handoff(self):
        rows = package.read(profiles.GENERATION_PROOF.with_name('profiles.json'))['profiles']
        before, after = rows['primary']['sdk'], rows['light']['sdk']
        profiles.check_generation_result(after, before)
        for key, value in [('answer', '999'), ('file_sha256', '0' * 64),
                           ('ids', list(reversed(after['recovered']['ids']))),
                           ('peer', 200), ('sibling', 200)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                changed['recovered'][key] = value
                profiles.check_generation_result(changed, before)
        for key in ['thread', 'file', 'partial', 'received']:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                changed['source'][key] += 'x'
                profiles.check_generation_result(changed, before)

    def test_duplex_unpacking_preserves_all_historical_record_formats(self):
        from tools.serve_recovery import profile_live_reuse as reuse
        self.assertTrue(set(f'q4t-profile-business-v{i}' for i in range(26, 62))
                        <= reuse.PACKED_DUPLEX_RECORDS)
        self.assertTrue({reuse.RECORD_FORMAT, reuse.BETA_RECORD_FORMAT, reuse.AGENT_RECORD_FORMAT}
                        .isdisjoint(reuse.PACKED_DUPLEX_RECORDS))


class PublicSummaryHandoff(unittest.TestCase):
    def test_changed_historical_digest_call_or_followup_is_rejected(self):
        from tools.serve_responses import chatkit_workflow_profiles as workflows
        rows = package.read(workflows.PROOF.with_name('profiles.json'))['profiles']
        before, after = rows['primary']['sdk'], rows['light']['sdk']
        workflows.check(after, before)
        for where, key, value in [
                ('archive', 'groups_sha256', '0' * 64),
                ('archive', 'items', before['archive']['items'][1:]),
                ('source', 'thread', before['archive']['thread']),
                ('source', 'groups', []),
                ('recovered', 'before_sha256', '0' * 64),
                ('recovered', 'answer', '5'),
                ('recovered', 'item', 'fc_unrelated'),
                ('recovered', 'call', 'call_unrelated'),
                ('recovered', 'ids', list(reversed(after['recovered']['ids']))),
                ('recovered', 'duplicate', 200),
                ('recovered', 'peer', 200),
                ('recovered', 'sibling', 200)]:
            with self.subTest(where=where, key=key), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                changed[where][key] = value
                workflows.check(changed, before)

    def test_unverified_history_does_not_create_a_partial_store(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from tools.serve_responses import chatkit_workflow_profiles as workflows
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            state = root / 'state'
            source.mkdir()
            state.mkdir()
            for name in package.read(workflows.HISTORY)['records']:
                (source / name).write_bytes(b'modified history')
            with self.assertRaises(RuntimeError):
                workflows.restore_history(state, source)
            self.assertEqual(list(state.iterdir()), [])
            store = state / 'application-objects'
            store.mkdir()
            sentinel = store / 'retained'
            sentinel.write_bytes(b'existing')
            with self.assertRaises(RuntimeError):
                workflows.restore_history(state, source)
            self.assertEqual(sentinel.read_bytes(), b'existing')


class ImageAttachmentHandoff(unittest.TestCase):
    def test_reject_changed_image_history_file_or_access(self):
        from tools.serve_responses import chatkit_image_profiles as images
        rows = package.read(images.PROOF.with_name('profiles.json'))['profiles']
        before, after = rows['primary']['sdk'], rows['light']['sdk']
        images.check(after, before)
        for section, key, value in [
                ('archive', 'preview_sha256', '0' * 64),
                ('archive', 'items', list(reversed(after['archive']['items']))),
                ('upload', 'preview_sha256', 'f' * 64),
                ('upload', 'file', before['upload']['file']),
                ('upload', 'source_sha256', 'f' * 64),
                ('upload', 'before', 200), ('upload', 'peer', 200),
                ('recovered', 'file', after['upload']['file']),
                ('recovered', 'source_sha256', '0' * 64),
                ('recovered', 'after_delete', 200), ('recovered', 'peer', 200)]:
            with self.subTest(section=section, key=key), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                changed[section][key] = value
                images.check(changed, before)
        for key in ('peer', 'sibling', 'source_deleted'):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                images.check(dict(after, **{key:200}), before)
