"""Failure-sensitive business driver checks and the real read-only TLS peer."""
import copy
import http.client
import json
import os
from pathlib import Path
import ssl
import tempfile
import types
import unittest

from tools.examples.project_records import ProjectRecords
from tools.serve_responses import mcp_acceptance as driver
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_responses.test_contract import errors


class McpAcceptance(unittest.TestCase):
    def test_requests_are_real_sdk_shapes_with_frozen_budget(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=8, controls=2000, seconds=900))
        for name, row in driver.requests().items():
            self.assertEqual([(list(e.path), e.message[:100]) for e in errors(row, 'response_create')], [], name)
            self.assertLessEqual(row['max_output_tokens'], 384)
        self.assertNotIn('require_approval', driver.requests()['approval']['tools'][0])
        self.assertEqual(driver.requests()['tunnel']['tools'][0]['tunnel_id'], 'tunnel_project')
        self.assertTrue(driver.requests()['tunnel']['stream'])

    def test_approval_uses_actual_identity_and_cannot_mutate_original_request(self):
        stage = object.__new__(driver.Stage)
        stage.plan = dict(requests=driver.requests())
        stage.peer = types.SimpleNamespace(url='https://localhost:18443/mcp', secret='cpu-canary')
        original = copy.deepcopy(stage.plan)
        approval = dict(id='resp_original', output=[dict(type='mcp_approval_request',
            id='mcpr_original', server_label='project', name='read_project_record',
            arguments=json.dumps(peer.ARGUMENTS))])
        for allowed in [True, False]:
            request = stage.follow(approval, allowed)
            self.assertEqual(request['previous_response_id'], 'resp_original')
            self.assertEqual(request['input'], [dict(type='mcp_approval_response',
                approval_request_id='mcpr_original', approve=allowed)])
            self.assertEqual(errors(request, 'response_create'), [])
        self.assertEqual(stage.plan, original)
        for field, value in [('server_label', 'other'), ('name', 'other'), ('arguments', '{}')]:
            bad = copy.deepcopy(approval)
            bad['output'][0][field] = value
            with self.assertRaises(RuntimeError):
                stage.follow(bad, True)
        bad = copy.deepcopy(approval)
        bad['output'].append(bad['output'][0])
        with self.assertRaises(RuntimeError):
            stage.follow(bad, True)


class ProjectPeer(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        root.mkdir(parents=True, exist_ok=True)
        self.output = Path(tempfile.mkdtemp(prefix='mcp-business-peer-', dir=root))
        self.peer = peer.ProjectMcp(driver.ROOT, self.output, 0)
        self.addCleanup(self.peer.close)
        self.context = ssl.create_default_context(cafile=str(self.peer.ca[0]))

    def rpc(self, method, params=None, *, session=None, secret=None):
        connection = http.client.HTTPSConnection('localhost', self.peer.server.server_port,
                                                context=self.context, timeout=3)
        try:
            headers = {'Authorization': 'Bearer '+(self.peer.secret if secret is None else secret),
                       'Content-Type': 'application/json'}
            if session:
                headers['MCP-Session-Id'] = session
            connection.request('POST', '/mcp', json.dumps(dict(jsonrpc='2.0', id='original',
                method=method, params=params or {})), headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def test_tls_auth_actual_document_and_exact_resource_target(self):
        self.assertEqual(self.rpc('initialize', secret='wrong')[0], 403)
        self.assertEqual(self.peer.snapshot()['methods'], [])
        status, headers, body = self.rpc('initialize')
        self.assertEqual((status, body['id']), (200, 'original'))
        session = headers['MCP-Session-Id']
        self.assertEqual(self.rpc('tools/list', session=session)[2]['result'], peer.catalog())
        self.assertEqual(self.rpc('tools/call', dict(name='read_project_record',
            arguments=dict(peer.ARGUMENTS, path='/etc/passwd')), session=session)[0], 400)
        self.assertEqual(self.peer.calls, [])
        body = self.rpc('tools/call', dict(name='read_project_record', arguments=peer.ARGUMENTS),
                        session=session)[2]
        record = ProjectRecords(driver.ROOT).read(peer.ARGUMENTS)
        self.assertEqual(body['result']['structuredContent'], record)
        self.assertEqual(json.loads(body['result']['content'][0]['text']), record)
        self.assertEqual(self.rpc('resources/read', dict(uri='file:///etc/passwd'), session=session)[0], 400)
        resource = self.rpc('resources/read', dict(uri=peer.URI), session=session)[2]
        self.assertEqual(resource['result']['contents'][0]['text'], record['source']['text'])
        self.assertEqual((len(self.peer.calls), len(self.peer.reads)), (1, 1))

    def test_dropped_receipt_follows_one_actual_read_and_session_can_be_closed(self):
        session = self.rpc('initialize')[1]['MCP-Session-Id']
        self.peer.drop_next = True
        with self.assertRaises(http.client.RemoteDisconnected):
            self.rpc('tools/call', dict(name='read_project_record', arguments=peer.ARGUMENTS),
                     session=session)
        self.assertEqual(len(self.peer.calls), 1)
        self.assertEqual(len(self.peer.reads), 0)
        connection = http.client.HTTPSConnection('localhost', self.peer.server.server_port,
                                                context=self.context, timeout=3)
        try:
            connection.request('DELETE', '/mcp', headers={'MCP-Session-Id': session,
                'Authorization': 'Bearer '+self.peer.secret})
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.peer.snapshot()['live_sessions'], 0)
        self.assertEqual(self.rpc('tools/list', session=session)[0], 404)


if __name__ == '__main__':
    unittest.main()
