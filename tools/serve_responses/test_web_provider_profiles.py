"""Check qualification evidence against actual native Responses transcripts."""
import copy
import json
import unittest

from tools.serve_responses import web_provider_profiles as provider
from tools.serve_service import package


class WebHandoff(unittest.TestCase):
    def setUp(self):
        folder = provider.EVIDENCE
        self.source = package.read(folder/'device.json')['source']
        original = package.read(folder/'responses.wire')
        events = [json.loads(line[5:]) for line in (folder/'responses-offline.wire').read_bytes().splitlines()
                  if line.startswith(b'data:') and line[5:].strip()!=b'[DONE]']
        offline = events[-1]['response']
        error = {'type':'invalid_request_error','code':'object_not_found','param':None}
        self.primary = dict(role='primary', request=dict(store=True,tools=[{}]),
            foreign_status=404,missing_status=404,foreign_error=error,missing_error=error.copy(),
            created_id=original['id'],response=original)
        self.light = dict(self.primary,role='light',request=dict(store=True,tools=[dict(external_web_access=False)]),
                          created_id=offline['id'],response=offline,retrieved_id=original['id'],retrieved=original)

    def test_actual_live_and_offline_outputs(self):
        provider.check('primary',self.primary,self.source)
        provider.check('light',self.light,self.source,self.primary)

    def test_source_offset_owner_and_handoff_mutations(self):
        for mutate in (
                lambda row:row.update(foreign_status=200),
                lambda row:row['foreign_error'].update(code='private_id'),
                lambda row:row['request']['tools'][0].update(external_web_access=True),
                lambda row:row.update(retrieved_id='resp_wrong'),
                lambda row:row['retrieved']['output'][0].update(status='failed'),
                lambda row:row['response']['output'][0]['action']['sources'].clear(),
                lambda row:row['response']['output'][-1]['content'][0]['annotations'][0].update(end_index=0)):
            bad = copy.deepcopy(self.light)
            mutate(bad)
            with self.assertRaises(RuntimeError):
                provider.check('light',bad,self.source,self.primary)
