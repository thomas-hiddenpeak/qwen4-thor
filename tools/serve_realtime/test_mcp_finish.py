"""Counterexamples for the remaining fault jobs and resumable remote ownership."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_realtime import mcp_checks as c, mcp_lifecycle
from tools.serve_realtime.test_mcp_checks import fixture


def sample():
    receipt, _, remote, source, before, after = fixture(c.MODES[1])
    generated, ordinary = receipt['responses'][0], receipt['responses'][3]
    actual = dict(generated['output'][0], error=receipt['tools'][1]['error'])
    events = []
    for row, identity, prompt in ((generated, 'user_mcp_read', c.READ_PROMPT),
                                  (ordinary, 'user_mcp_after_fault', c.ORDINARY_PROMPT)):
        events.extend([
            dict(type='conversation.item.done', item=dict(type='message', id=identity,
                role='user', content=[dict(type='input_text', text=prompt)])),
            dict(type='response.created', response=dict(id=row['id']))])
        if row is generated:
            events.append(dict(type='response.mcp_call_arguments.delta', response_id=row['id'],
                               item_id=actual['id'], delta=actual['arguments']))
        events.append(dict(type='response.done', response=copy.deepcopy(row)))
        if row is generated:
            events.extend([dict(type='response.mcp_call.in_progress', item_id=actual['id']),
                           dict(type='response.output_item.done', item=copy.deepcopy(actual)),
                           dict(type='conversation.item.done', item=copy.deepcopy(actual))])
    receipt.update(mode='unknown-only', responses=[generated, ordinary], tools=[actual],
                   quality=receipt['quality'][1:], events=events)
    remote.update(calls=remote['calls'][:1], methods=['tools/call'])
    return [receipt, copy.deepcopy(events), remote, source, before,
            dict(after, admitted=2, settled=28)]


class UnknownReceipt(unittest.TestCase):
    def test_real_read_unknown_and_new_job_settle(self):
        result = c.observe_unknown(*sample())
        self.assertEqual(result['model_jobs'], 2)
        self.assertEqual(result['unknown_outcomes'], 1)

    def test_model_refusal_is_not_an_executed_tool(self):
        data = sample()
        data[0]['responses'][0]['output'] = [dict(type='message', content=[
            dict(type='output_text', text='I refuse')])]
        with self.assertRaises(Exception):
            c.observe_unknown(*data)

    def test_lost_receipt_cannot_be_success_or_replayed(self):
        for change in ('replay', 'success', 'no_read', 'usage'):
            data = sample()
            if change == 'replay': data[2]['methods'].append('tools/call')
            if change == 'success': data[0]['tools'][0]['output'] = 'invented'
            if change == 'no_read': data[2]['calls'] = []
            if change == 'usage': data[5]['settled'] += 1
            with self.assertRaises(Exception): c.observe_unknown(*data)

    def test_user_tasks_and_post_generation_dispatch_required(self):
        for identity in ('user_mcp_read', 'user_mcp_after_fault'):
            data = sample()
            data[0]['events'] = [e for e in data[0]['events'] if e.get('item', {}).get('id') != identity]
            data[1] = copy.deepcopy(data[0]['events'])
            with self.assertRaises(Exception): c.observe_unknown(*data)
        data = sample(); events = data[0]['events']
        sent = next(e for e in events if e['type'] == 'response.mcp_call.in_progress')
        events.remove(sent); events.insert(0, sent); data[1] = copy.deepcopy(events)
        with self.assertRaises(Exception): c.observe_unknown(*data)


class RetainedSessions(unittest.TestCase):
    def setUp(self):
        root = Path('.q4t-work/serve-realtime-mcp-20261120/sdk-fixtures')
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.scope = 'client:fixture'
        self.owner = hashlib.sha256(self.scope.encode()).hexdigest()
        self.record = dict(format=1, owner=self.owner, kind='tool_run', id='mcpl_fixture',
            revision=1, created_at=100, expires_at=3700, data=dict(format='q4t.mcp.catalog.v1',
                sealed='q4t1:'+'a'*32+':'+'b'*24+':'+'c'*32+':'+'d'*20))
        self.events = [dict(type='mcp_list_tools.completed', item_id='mcpl_fixture')]
        self.remote = dict(methods=['initialize'], live_sessions=1, deleted_sessions=0)

    def write(self):
        digest = hashlib.sha256(json.dumps(self.record, separators=(',', ':')).encode()).hexdigest()
        path = self.directory/(self.owner+'.tool_run.mcpl_fixture.json')
        path.write_text(json.dumps(dict(record=self.record, sha256=digest)))
        return path

    def check(self):
        return mcp_lifecycle.retained(self.events, self.remote, self.directory, self.scope)

    def test_owned_finite_remote_session_need_not_be_deleted(self):
        self.write()
        self.assertEqual(self.check()['remote_sessions_retained'], 1)
        self.remote.update(live_sessions=0, deleted_sessions=1)
        self.assertEqual(self.check()['remote_sessions_retained'], 0)

    def test_orphan_missing_foreign_or_unbounded_catalog_rejected(self):
        self.write(); self.remote['live_sessions'] = 2
        with self.assertRaises(Exception): self.check()
        self.remote['live_sessions'] = 1
        self.record['expires_at'] = 0; self.write()
        with self.assertRaises(Exception): self.check()
        self.record['expires_at'] = 3700; self.record['owner'] = 'b'*64; self.write()
        with self.assertRaises(Exception): self.check()

    def test_corrupt_plaintext_and_unpublished_session_rejected(self):
        path = self.write(); wrapper = json.loads(path.read_text())
        wrapper['record']['expires_at'] = 3600; path.write_text(json.dumps(wrapper))
        with self.assertRaises(Exception): self.check()
        self.record['data']['sealed'] = 'plaintext'; self.write()
        with self.assertRaises(Exception): self.check()
        self.record['data']['sealed'] = 'q4t1:'+'a'*32+':'+'b'*24+':'+'c'*32+':'+'d'*20
        self.write(); self.events = []
        with self.assertRaises(Exception): self.check()


if __name__ == '__main__': unittest.main()
