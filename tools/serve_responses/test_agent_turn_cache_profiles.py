import copy
import unittest

from tools.serve_responses import agent_turn_cache_profiles as handoff
from tools.serve_service import package


class AgentTurnCacheHandoff(unittest.TestCase):
    def setUp(self):
        self.proof = package.read(handoff.PROOF)
        obs = self.proof['observations']
        revision = self.proof['default_revision']
        rows = [dict(turn, answer=answer, root_sha256='1'*64, child_sha256='2'*64,
                     items_sha256='3'*64) for turn, answer in zip(
                         obs['turns'], [obs['first_answer'],obs['followup_answer']], strict=True)]
        self.primary = dict(phase='primary', sdk=obs['sdk'], session=obs['session'],
                            child=obs['child'], revision=revision, rows=rows,
                            cross_client_404=4, next_ttl='12h')
        self.light = copy.deepcopy(self.primary)
        self.light.pop('next_ttl')
        self.light.update(phase='light', continuation=dict(
            root='turn_'+'a'*32, child='turn_'+'b'*32, ttl='12h',
            answer=str(revision+2), items_sha256='4'*64, completed_sse=2))

    def test_same_owner_old_turns_and_new_work_are_required(self):
        handoff.check(self.light, self.proof, 'light', self.primary)
        for change in (dict(ttl='30m'),dict(answer='1'+self.light['continuation']['answer']),
                       dict(root=self.primary['rows'][0]['root']),dict(completed_sse=1)):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                trial = copy.deepcopy(self.light)
                trial['continuation'].update(change)
                handoff.check(trial, self.proof, 'light', self.primary)

    def test_changed_old_items_or_missing_tenant_isolation_fails(self):
        for field, value in [('items_sha256','5'*64),('ttl','30m')]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                trial = copy.deepcopy(self.light)
                trial['rows'][0][field] = value
                handoff.check(trial, self.proof, 'light', self.primary)
        for change in [dict(cross_client_404=3), dict(child='foreign')]:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                handoff.check(dict(self.primary,**change), self.proof, 'primary')
