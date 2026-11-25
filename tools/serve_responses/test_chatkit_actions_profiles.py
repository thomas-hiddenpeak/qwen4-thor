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
