"""Guard the Agent turn and cache policy evidence used to qualify a recovery package."""
import unittest
from copy import deepcopy

from tools.serve_recovery import ledger, profile_live_reuse as reuse, profile_record
from tools.serve_responses import agent_session_control_profiles as controls
from tools.serve_service import package


class AgentSessionControlProfiles(unittest.TestCase):
    def test_recovery_requires_original_turn_and_new_scoped_turn(self):
        rows = package.read(controls.PROOF.with_name('profiles.json'))['profiles']
        before, after = rows['primary']['sdk'], rows['light']['sdk']
        controls.check(after, before)
        for path, value in [
            (('source', 'turn'), after['resumed']['turn']),
            (('resumed', 'item'), before['source']['item']),
            (('resumed', 'ttl'), '12h'), (('resumed', 'cleared'), False),
            (('restored_ttl',), '30m'), (('padded',), False), (('peer',), 200),
            (('deltas',), 0), (('deltas',), True),
        ]:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                changed = deepcopy(after)
                target = changed if len(path) == 1 else changed[path[0]]
                target[path[-1]] = value
                controls.check(changed, before)

    def test_record_requires_matching_source_and_size(self):
        value = dict(format=reuse.SESSION_CONTROL_RECORD,
                     reused_business=dict(format=reuse.SESSION_CONTROL_FORMAT))
        profile_record.shape(value, profile_record.LEGACY_BYTES + 1)
        with self.assertRaises(ledger.LedgerError):
            profile_record.shape(value, profile_record.MAX_BYTES + 1)
        value['reused_business']['format'] = reuse.CHATKIT_WIDGET_FORMAT
        with self.assertRaises(ledger.LedgerError):
            profile_record.shape(value, profile_record.LEGACY_BYTES + 1)
