"""Public actual failures and counterexamples for MCP qualification composition."""
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tarfile
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_record, profile_evidence
from tools.serve_recovery import profile_mcp_codec as codec
from tools.serve_recovery import profile_mcp_evidence as evidence
from tools.serve_recovery import profile_mcp_reuse as reuse
from tools.serve_recovery.profile_mcp_source import REVIEW

ROOT = Path(__file__).resolve().parents[2]


def leaf(archive, member):
    path = ROOT/'docs/evidence'/archive/'records.tar.gz'
    with tarfile.open(path, 'r:gz') as stream:
        item = stream.getmember(member)
        assert item.isfile() and item.size <= 8 << 20
        return json.load(stream.extractfile(item))


def actual(name):
    first = 'serve-realtime-mcp-sdk-followup-cpu-20261120'
    second = 'serve-realtime-mcp-lifecycle-cpu-20261120'
    final = 'serve-realtime-mcp-remaining-http-20261120'
    if name == 'primary':
        original = dict(session=leaf(first, 'sdk-business-02/primary-mcp.sdk.json'),
            remote=leaf(first, 'sdk-business-02/primary-peer-results.json'), before=None, after=None)
        repair = leaf(second, 'sdk-business-03/primary-mcp-observed.json')
    else:
        original = leaf(second, 'sdk-business-03/light-mcp-observed.json')
        repair = leaf(final, 'sdk-business-04/light-mcp-observed.json')
    return dict(source=copy.deepcopy(original['remote']['calls'][0]['source']),
                original=original, repair=repair)


def envelope(name, data):
    return dict(format=evidence.FORMAT, profile=name,
                source_review_sha256=ledger.digest(REVIEW), witness=codec.pack(data))


class Codec(unittest.TestCase):
    def test_exact_roundtrip_keeps_existing_file_limit(self):
        value = dict(original_failure='没有改写原错误', events=list(range(100)))
        self.assertEqual(codec.unpack(codec.pack(value)), value)
        self.assertEqual(profile_record.MAX_BYTES, 8 << 20)

    def test_truncation_trailing_data_hash_and_expansion_rejected(self):
        original = codec.pack(dict(data='x'*10000))
        for change in ('truncate', 'trailing', 'hash', 'short', 'large'):
            value = copy.deepcopy(original)
            if change == 'truncate': value['data'] = value['data'][:-8]
            if change == 'trailing':
                value['data'] = base64.b64encode(base64.b64decode(value['data'])+gzip.compress(b'x')).decode()
            if change == 'hash': value['sha256'] = '0'*64
            if change == 'short': value['bytes'] = 5
            if change == 'large': value['bytes'] = codec.MAX_PLAIN + 1
            with self.assertRaises(Exception): codec.unpack(value)

    def test_duplicate_json_keys_cannot_hide_an_original_failure(self):
        raw = b'{"status":"FAIL","status":"PASS"}'
        value = dict(format=codec.FORMAT, bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(), data=base64.b64encode(gzip.compress(raw)).decode())
        with self.assertRaises(ValueError): codec.unpack(value)


class ActualEvidence(unittest.TestCase):
    def test_both_profiles_validate_originals_and_actual_repairs(self):
        for name in ('primary', 'light'):
            data = actual(name); original = copy.deepcopy(data)
            evidence.business(envelope(name, data))
            self.assertEqual(data, original)
            self.assertEqual(data['original']['session']['status'], 'FAIL')

    def test_no_success_relabelling_or_false_terminal(self):
        for name in ('primary', 'light'):
            for change in ('status', 'terminal'):
                data = actual(name)
                if change == 'status': data['original']['session']['status'] = 'PASS'
                else: data['original']['session']['responses'][-1]['status'] = 'cancelled'
                with self.assertRaises(Exception):
                    evidence.original(data['original'], name, data['source'])

    def test_read_approval_and_source_cannot_be_fabricated(self):
        for change in ('read', 'approval', 'source'):
            data = actual('primary')
            if change == 'read': data['original']['remote']['calls'] = []
            if change == 'source': data['source']['sha256'] = 'b'*64
            if change == 'approval':
                data['original']['session']['tools'][1]['approval_request_id'] = 'foreign'
            with self.assertRaises(Exception):
                evidence.original(data['original'], 'primary', data['source'])

    def test_witness_profile_digest_and_review_cannot_be_rebound(self):
        value = envelope('primary', actual('primary'))
        for changed in (dict(value, profile='light'), dict(value, source_review_sha256='0'*64),
                        envelope('primary', actual('light'))):
            with self.assertRaises(ValueError): evidence.business(changed)


class Capabilities(unittest.TestCase):
    @staticmethod
    def value():
        return leaf('serve-realtime-mcp-remaining-http-20261120',
                    'sdk-business-04/light-capabilities.json')

    def test_real_mcp_capability_passes_complete_shared_gate(self):
        value = self.value()
        self.assertTrue(profile_evidence.conversation_enabled(value))
        self.assertTrue(profile_evidence.duplex_enabled(value))
        self.assertEqual(profile_evidence.format_for({}, capabilities=value),
                         profile_evidence.MCP_FORMAT)

    def test_previous_duplex_declaration_remains_exactly_accepted(self):
        value = self.value()
        chat = value['realtime']['conversation']; chat.pop('mcp')
        chat['pending_execution'] = ['mcp', 'tracing']
        self.assertTrue(profile_evidence.conversation_enabled(value))
        self.assertEqual(profile_evidence.format_for({}, capabilities=value),
                         profile_evidence.DUPLEX_FORMAT)

    def test_replay_or_weakly_typed_capability_cannot_pass_shared_gate(self):
        for field, replacement in (('automatic_tool_replay', True), ('approval', 1),
                                   ('maximum_servers', 128)):
            value = self.value()
            value['realtime']['conversation']['mcp'][field] = replacement
            with self.assertRaises(ValueError): profile_evidence.conversation_enabled(value)
            with self.assertRaises(ValueError): reuse.old_capabilities(value)


class SourceReuse(unittest.TestCase):
    def setUp(self):
        identity = dict(binary_sha256=REVIEW['candidate_binary'],
                        protocol_sha256=REVIEW['candidate_protocol'], backend='unchanged')
        capabilities = dict(protocol_source_sha256=REVIEW['candidate_protocol'],
            realtime=dict(conversation=dict(mcp=copy.deepcopy(evidence.CAPABILITY),
                                           pending_execution=['tracing'])))
        self.value = dict(format='q4t-profile-business-v21', profile='primary', identity=identity,
            capacity=dict(max_seq=1), capabilities=capabilities,
            business=dict(existing=dict(actual=1), realtime_mcp=dict(profile='primary')))
        source = dict(identity=ledger.digest(dict(identity, binary_sha256=REVIEW['previous_binary'],
            protocol_sha256=REVIEW['previous_protocol'])), capacity=ledger.digest(self.value['capacity']),
            capabilities=ledger.digest(reuse.old_capabilities(capabilities)),
            business=dict(existing=ledger.digest(self.value['business']['existing'])))
        for patch in (mock.patch.object(reuse, 'SOURCES', dict(primary=source)),
                      mock.patch.object(reuse, 'OBSERVED', dict(primary={})),
                      mock.patch.object(evidence, 'business')):
            patch.start(); self.addCleanup(patch.stop)

    def test_new_evidence_does_not_replace_existing_business(self):
        value = reuse.attach(self.value); reuse.validate(value)
        for field in ('business', 'identity', 'capacity'):
            changed = copy.deepcopy(value)
            if field == 'business': changed[field]['existing']['actual'] = 2
            if field == 'identity': changed[field]['backend'] = 'changed'
            if field == 'capacity': changed[field]['max_seq'] = 2
            with self.assertRaises(ValueError): reuse.validate(changed)

    def test_capability_and_profile_owner_changes_rejected(self):
        value = reuse.attach(self.value)
        changed = copy.deepcopy(value)
        changed['capabilities']['realtime']['conversation']['mcp']['automatic_tool_replay'] = True
        with self.assertRaises(ValueError): reuse.validate(changed)
        changed = copy.deepcopy(value); changed['business']['realtime_mcp']['profile'] = 'light'
        with self.assertRaises(ValueError): reuse.validate(changed)

    def test_larger_qualification_remains_bounded_and_format_scoped(self):
        value = dict(format='q4t-profile-business-v21', reused_business=dict(format=reuse.FORMAT))
        profile_record.shape(value, 3 << 20)
        with self.assertRaises(ValueError): profile_record.shape(value, (8 << 20) + 1)
        with self.assertRaises(ValueError): profile_record.shape(dict(value, format='arbitrary'), 3 << 20)


if __name__ == '__main__': unittest.main()
