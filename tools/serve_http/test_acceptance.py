"""N05 harness limits and real frame completion; CPU only."""
import copy
import io
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_http import acceptance as a


class DriverTests(unittest.TestCase):
    def test_pending_release_requires_exact_identity_and_preserves_default_gate(self):
        metadata=dict(validation_only=True,acceptance='VALIDATION_ONLY_N05',binary='original')
        decision=dict(status='PENDING_REAL_BUSINESS_N05')
        original=mock.Mock();gate=a.validation_gate(original,dict(metadata=a.fingerprint(metadata),
                                                                decision=a.fingerprint(decision)))
        gate(metadata,decision);original.assert_not_called()
        for change in [dict(metadata,binary='changed'),dict(metadata,acceptance='PASS_FAKE')]:
            with self.assertRaises(ValueError):gate(change,decision)
        with self.assertRaises(ValueError):gate(metadata,dict(decision,status='PASS_FAKE'))
        gate(dict(validation_only=False),decision);original.assert_called_once()

    def test_chunk_end_required_and_next_response_is_not_consumed(self):
        first=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n'
        second=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]'
        reader=io.BytesIO(first+second)
        self.assertEqual(a.read_response(reader)[2:],(b'{}',first))
        self.assertEqual(a.read_response(reader)[2:],(b'[]',second))
        for raw in [first[:-1],first.replace(b'2\r\n{}\r\n0',b'3\r\n{}\r\n0'),
                    first.replace(b'Transfer-Encoding: chunked',b'Transfer-Encoding: chunked\r\nContent-Length: 2')]:
            with self.assertRaises(ValueError):a.read_response(io.BytesIO(raw))
        with self.assertRaises(ValueError):
            a.read_response(io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 999999999\r\n\r\n'))

    def test_continue_rejection_never_sends_body_and_valid_continue_is_chunked(self):
        sock=mock.Mock()
        with self.assertRaises(ValueError):a.exchange(sock,io.BytesIO(
            b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n'),'/v1/chat/completions',{'p':'secret'},
            {'Authorization':'Bearer test'},chunked=True)
        self.assertEqual(sock.sendall.call_count,1)
        self.assertNotIn(b'secret',sock.sendall.call_args.args[0])
        sock.reset_mock()
        reply=b'HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'
        self.assertEqual(a.exchange(sock,io.BytesIO(reply),'/v1/chat/completions',{'p':1},{},chunked=True)[2],b'{}')
        self.assertEqual(sock.sendall.call_args_list[-1].args[0],b'0\r\n\r\n')

    def test_generation_budget_is_reserved_before_any_network_write(self):
        stage=a.Stage.__new__(a.Stage)
        stage.budget=SimpleNamespace(reserve=mock.Mock(side_effect=RuntimeError('exhausted')))
        sock=mock.Mock()
        with self.assertRaisesRegex(RuntimeError,'exhausted'):
            stage.generate('blocked',{'p':1},sock,io.BytesIO())
        sock.sendall.assert_not_called()
        self.assertEqual(a.LIMITS,dict(starts=1,chats=10,probes=0,controls=2000,seconds=600))

    def test_prefix_resume_requires_same_cleaned_failure_and_charged_counts(self):
        root=a.ROOT/'.q4t-work/serve-http-20261109/resume-cpu';root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            directory=Path(directory)
            counts=dict(starts=1,chats=5,probes=0,controls=165)
            prior=dict(status='FAILED',cleanup_failure=None,package={'pinned':'package'},
                failure='Node bridge failed before response',budget=dict(counts=counts),
                models={'1':{'pid':123}},seconds=48)
            plan=dict(resume_after_prefix=True,package=prior['package'],prior_counts=counts,
                      prior_seconds=48,prior_evidence=[])
            for key,value in [('execution',prior),('guard',dict(status='COMMAND_FAILED')),
                              ('stopped',dict(desired='STOPPED',active={'model':None}))]:
                path=directory/(key+'.json');path.write_text(json.dumps(value));plan['prior_'+key]=str(path)
            a.verify_prior(plan)
            for key,value in [('prior_counts',dict(counts,chats=4)),('prior_seconds',47),
                              ('package',{'another':'package'})]:
                with self.assertRaises(ValueError):a.verify_prior(dict(plan,**{key:value}))
            file=Path(plan['prior_execution']);file.write_text(json.dumps(dict(prior,cleanup_failure='unknown')))
            with self.assertRaises(ValueError):a.verify_prior(plan)
            file.write_text(json.dumps(prior));plan['prior_evidence']=[dict(path=str(file),sha256=hashlib.sha256(file.read_bytes()).hexdigest())]
            file.write_text(json.dumps(dict(prior,failure='unrelated')))
            with self.assertRaises(ValueError):a.verify_prior(plan)


if __name__=='__main__':unittest.main()
