"""Offline guardrails for the composite driver; no sockets, models or CUDA."""
import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zlib

from tools.serve_media_composite import fixtures, stage, verify as v
from tools.serve_protocol.check_contract import load_validators
from jsonschema import Draft202012Validator


class Reply:
    def __init__(self, pieces, status=200):
        self.pieces, self.status, self.reads = list(pieces), status, 0
    def getheaders(self):
        return [('X-Request-Id', 'fixture-request'), ('Content-Type',
                 'text/event-stream' if self.status == 200 else 'application/json')]
    def read1(self, maximum):
        self.reads += 1
        return self.pieces.pop(0) if self.pieces else b''


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=stage.BASE)
        cls.root = Path(cls.temporary.name)
        cls.folder = cls.root/'fixtures'
        fixtures.prepare(cls.folder)
        cls.rows = stage.h.read(cls.folder/'cases.json')
        cls.schema = Draft202012Validator(stage.h.read(v.ROOT/'docs/serve-transport-v1.schema.json'))
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def wire(self, name, pieces, *, param='messages', expected='messages'):
        folder = self.root/name; folder.mkdir()
        reply = Reply(pieces)
        connection = Mock()
        connection.getresponse.return_value = reply
        body = dict(request_id='fixture-request', stream=True)
        with patch.object(v.http.client, 'HTTPConnection', return_value=connection):
            result = v.capture_failure(folder, 'fault', 1, body, 200,
                'media_prefill_limit', expected, self.schema)
        connection.request.assert_called_once()
        connection.close.assert_called_once()
        return folder, reply, result

    @staticmethod
    def error(param='messages'):
        return ('data: '+json.dumps(dict(error=dict(message='fixture',
            type='invalid_request_error', code='media_prefill_limit', param=param)))+'\n\n').encode()

    def test_complete_scope_keeps_all_unexecuted_ownership_scenarios(self):
        self.assertEqual(v.SCENARIOS[:9], v.old.SCENARIOS)
        self.assertEqual(len({row[0] for row in v.SCENARIOS}), 11)
        self.assertEqual(stage.PLANNED, dict(generations=17+11+2+2,
                                            negatives=8+11+1+1, starts=9+2))
        self.assertTrue(all(stage.PLANNED[k] <= stage.LIMITS[k] for k in stage.PLANNED))

    def test_fault_environment_is_scoped_and_does_not_inherit_preload(self):
        with patch.dict(os.environ, dict(LD_PRELOAD='bad', Q4T_SERVE_FAULT='bad')):
            admission = v.environment('media-admission')
            cancel = v.environment('media-cancel', self.root/'gate')
        self.assertNotIn('LD_PRELOAD', admission)
        self.assertNotIn('Q4T_SERVE_FAULT', cancel)
        self.assertEqual(admission['Q4T_SERVE_DEVICE_FAULT'], 'vision.pixels_allocate')
        self.assertEqual(admission['Q4T_SERVE_REQUEST'], fixtures.FAULT_REQUEST)
        self.assertNotIn('Q4T_SERVE_DEVICE_FAULT', cancel)
        self.assertEqual(cancel['Q4T_SERVE_PAUSE'], 'vision.after_forward')
        self.assertEqual(cancel['Q4T_SERVE_REQUEST'], fixtures.CANCEL_REQUEST)
        with self.assertRaises(RuntimeError): v.environment('media-cancel')

    def test_fixed_negative_wire_and_text_contract_are_independent(self):
        self.assertEqual([r['name'] for r in self.rows], list(fixtures.NAMES))
        text = load_validators()['chat']
        for row in self.rows:
            body = copy.deepcopy(row['body'])
            self.assertEqual(body['request_id'], fixtures.FAULT_REQUEST)
            self.assertEqual(row['status'], 200 if body['stream'] else 400)
            body['messages'][0]['content'] = 'negative media fixture projection'
            text.validate(body)
        self.assertEqual(sum(r['status'] == 200 for r in self.rows), 2)
        self.assertEqual(self.rows, fixtures.cases(self.folder))

    def test_hidden_idat_expansion_is_bounded_in_fixture_generation(self):
        raw = (self.folder/'decoder-quota.png').read_bytes()
        self.assertLess(len(raw), 1024*1024)
        self.assertEqual(struct.unpack('>II', raw[16:24]), (1, 1))
        length = struct.unpack('>I', raw[33:37])[0]
        self.assertEqual(raw[37:41], b'IDAT')
        decompress = zlib.decompressobj()
        prefix = decompress.decompress(raw[41:41+length], 1024*1024)
        self.assertEqual(prefix, b'\0'*(1024*1024))
        self.assertFalse(decompress.eof)
        self.assertTrue(decompress.unconsumed_tail)
        contract = stage.h.read(self.folder/'fixture-contract.json')
        self.assertEqual(contract['idat_expanded_bytes'], 256*1024*1024+1)

    def test_error_capture_reads_through_done_and_eof(self):
        pieces = [self.error(), b'data: [DONE]\n\n']
        folder, reply, result = self.wire('complete', pieces)
        self.assertEqual(reply.reads, 3)
        self.assertEqual((folder/'fault-wire.bin').read_bytes(), b''.join(pieces))
        self.assertTrue(stage.h.read(folder/'fault-http.json')['eof'])
        self.assertFalse(result['success'])

    def test_missing_or_duplicate_done_remains_a_failure(self):
        done = b'data: [DONE]\n\n'
        for index, pieces in enumerate(([self.error()], [self.error(), done, done],
                                        [self.error(), self.error(), done])):
            with self.subTest(index=index):
                with self.assertRaises(RuntimeError): self.wire('invalid-'+str(index), pieces)
                self.assertTrue(stage.h.read(self.root/f'invalid-{index}/fault-http.json')['eof'])

    def test_wrong_param_is_not_accepted_as_generic_rejection(self):
        with self.assertRaises(RuntimeError):
            self.wire('wrong-param', [self.error('wrong'), b'data: [DONE]\n\n'])
        self.assertTrue((self.root/'wrong-param/fault-wire.bin').exists())

    def test_attempt_limit_rejects_before_extra_work(self):
        folder = self.root/'attempts'; folder.mkdir()
        run = object.__new__(v.Run)
        run.out, run.deadline = folder, time.time()+30
        run.counts = {k: stage.LIMITS[k] for k in stage.PLANNED}
        run.counts['starts'] -= 1
        run.budget('starts')
        with self.assertRaises(RuntimeError): run.budget('starts')
        self.assertEqual(len(list(folder.iterdir())), 1)
        run.deadline = 0
        with self.assertRaises(RuntimeError): run.budget()

    def test_capability_must_match_actual_function_not_assumed_device(self):
        run = object.__new__(v.Run)
        run.controls = 0
        run.plan = dict(actual_attention_shared_bytes=47104)
        def reply(value): return dict(status=200, raw=json.dumps(value))
        policy = dict(attention_shared_bytes=47104, decoder_owned_bytes=fixtures.DECODER_LIMIT,
                      whole_server_ram_limit=False)
        value = dict(runtime=dict(media_enabled=True), media=policy)
        with patch.object(v.old.base.Run, 'http', return_value=reply(value)):
            run.http('GET', '/v1/capabilities')
        value['media']['attention_shared_bytes'] = 49152
        with patch.object(v.old.base.Run, 'http', return_value=reply(value)):
            with self.assertRaises(RuntimeError): run.http('GET', '/v1/capabilities')
        value['runtime']['media_enabled'] = False
        value['media']['attention_shared_bytes'] = None
        with patch.object(v.old.base.Run, 'http', return_value=reply(value)):
            run.http('GET', '/v1/capabilities')

    def test_failed_business_cannot_generate_release(self):
        output = self.root/'failed-stage'; (output/'business').mkdir(parents=True)
        stage.h.save(output/'business/result.json', dict(status='FAILED'))
        with patch.object(stage, 'OUTPUT', output):
            with self.assertRaises(RuntimeError): stage.release({})
        self.assertFalse((output/'release').exists())

    def delivery(self, name, elapsed, health_failure=False):
        # Real filesystem mutations are confined to synthetic pairs under this
        # temporary directory; GPU/service probes are replaced by mocks.
        root = self.root/name; (root/'build').mkdir(parents=True)
        output = root/'execution'; output.mkdir()
        release = root/'candidate'; release.mkdir()
        for filename in stage.a.NAMES:
            (root/'build'/filename).write_text('old-'+filename)
            (release/filename).write_text('new-'+filename)
        before = {p: stage.h.sha(root/'build'/p) for p in stage.a.NAMES}
        after = {p: stage.h.sha(release/p) for p in stage.a.NAMES}
        stage.h.save(release/'binding.json', dict(old=before, new=after))
        plan = dict(old_hashes=before, original_started=1000,
                    candidate=str(release/'q4t'), candidate_sha256=after['q4t'])
        with ExitStack() as stack:
            for owner, field, value in ((stage, 'ROOT', root), (stage, 'OUTPUT', output),
                (stage, 'PACKAGE', root/'package'),
                (stage.a, 'MARKER', root/'build/recovery.json')):
                stack.enter_context(patch.object(owner, field, value))
            stack.enter_context(patch.object(stage.h, 'require_guard'))
            stack.enter_context(patch.object(stage, 'check', return_value=plan))
            stack.enter_context(patch.object(stage.time, 'time', return_value=1000+elapsed))
            stack.enter_context(patch.object(stage.a.acceptance, 'install_interrupt_handlers'))
            stack.enter_context(patch.object(stage.a, 'require_idle_gpu'))
            stack.enter_context(patch.object(stage.a, 'state_pair', return_value=before))
            stack.enter_context(patch.object(stage.h, 'verify_pair'))
            verifier = stack.enter_context(patch.object(stage, 'verifier',
                side_effect=[None, RuntimeError('synthetic installed health failure')]
                            if health_failure else None))
            publish = stack.enter_context(patch.object(stage, 'release', return_value=release))
            restore = stack.enter_context(patch.object(stage.a, 'restore'))
            error = None
            try: stage.execute_locked()
            except RuntimeError as caught: error = caught
        return root, stage.h.read(output/'execution.json'), error, publish, restore, verifier

    def test_delivery_uses_frozen_limit_beyond_old_literal_deadline(self):
        root, result, error, publish, restore, verifier = self.delivery('deadline-valid', 1600)
        self.assertIsNone(error)
        self.assertEqual(result['status'], 'HEALTH_CHECKED_PENDING_GUARD')
        self.assertEqual((root/'build/q4t').read_text(), 'new-q4t')
        publish.assert_called_once(); restore.assert_not_called()
        self.assertEqual(verifier.call_count, 2)

    def test_installation_reserve_stops_before_default_pair_mutation(self):
        root, result, error, publish, restore, verifier = self.delivery('deadline-reject', 2150)
        self.assertIn('installation time reserve', str(error))
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual((root/'build/q4t').read_text(), 'old-q4t')
        publish.assert_not_called(); restore.assert_not_called()
        self.assertEqual(verifier.call_count, 1)

    def test_installed_health_failure_invokes_known_pair_restore(self):
        root, result, error, publish, restore, verifier = self.delivery('health-fail', 1600, True)
        self.assertIn('synthetic installed health failure', str(error))
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['rollback'], 'RESTORED')
        restore.assert_called_once_with(root/'execution')
        self.assertFalse((root/'execution/finalized.json').exists())


if __name__ == '__main__': unittest.main()
