"""Public trace witnesses and counterexamples for exact qualification reuse."""
import copy
import hashlib
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_evidence, profile_record
from tools.serve_recovery import profile_mcp_codec as codec, profile_mcp_reuse
from tools.serve_recovery import profile_trace_evidence as evidence
from tools.serve_recovery import profile_trace_reuse as reuse
from tools.serve_recovery.profile_trace_source import REVIEW
from tools.serve_recovery.test_profile_mcp import leaf


def actual(name):
    return leaf('serve-realtime-tracing-http-20261121',
                'sdk-business-06/'+name+'-tracing-observed.json')


def capabilities(name='primary'):
    return leaf('serve-realtime-tracing-http-20261121',
                'sdk-business-06/'+name+'-capabilities.json')


class ActualTrace(unittest.TestCase):
    def test_both_actual_witnesses_validate_without_mutation(self):
        for name in ('primary', 'light'):
            value = actual(name); before = copy.deepcopy(value)
            self.assertEqual(evidence.business(evidence.attach(name, value))['profile'], name)
            self.assertEqual(value, before)

    def test_observed_capture_scope_usage_quality_and_deletion_cannot_change(self):
        for field in ('mode', 'quality', 'snapshot', 'operations'):
            value = actual('light'); value['session'][field] = None
            with self.assertRaises(ValueError): evidence.attach('light', value)
        with self.assertRaises(ValueError): evidence.attach('primary', actual('light'))

    def test_real_capabilities_select_v22_and_previous_v21_stays_valid(self):
        value = capabilities()
        self.assertTrue(profile_evidence.conversation_enabled(value))
        self.assertEqual(profile_evidence.format_for({}, capabilities=value),
                         profile_evidence.TRACING_FORMAT)
        old = reuse.old_capabilities(value)
        self.assertTrue(profile_evidence.conversation_enabled(old))
        self.assertEqual(profile_evidence.format_for({}, capabilities=old),
                         profile_evidence.MCP_FORMAT)

    def test_capture_or_core_dependency_cannot_be_hidden_in_capabilities(self):
        for field in ('capture_content', 'external_collector_required',
                      'readiness_dependency', 'model_restart_dependency'):
            value = capabilities(); value['realtime']['conversation']['tracing'][field] = True
            with self.assertRaises(ValueError): profile_evidence.conversation_enabled(value)
            with self.assertRaises(ValueError): reuse.old_capabilities(value)

    def test_file_limit_is_unchanged_and_explicitly_format_scoped(self):
        value = dict(format=profile_evidence.TRACING_FORMAT,
                     reused_business=dict(format=reuse.FORMAT))
        profile_record.shape(value, profile_record.MAX_BYTES)
        self.assertEqual(profile_record.MAX_BYTES, 8 << 20)
        for bad in (dict(value, format='unknown'), dict(value, reused_business={})):
            with self.assertRaises(ValueError): profile_record.shape(bad, 3 << 20)
        with self.assertRaises(ValueError): profile_record.shape(value, (8 << 20) + 1)


class ExactReuse(unittest.TestCase):
    def setUp(self):
        self.prior = dict(format='q4t-profile-business-v21', profile='primary',
            identity=dict(binary_sha256=REVIEW['previous_binary'],
                protocol_sha256=REVIEW['previous_protocol'], backend='unchanged'),
            capabilities=dict(protocol_source_sha256=REVIEW['previous_protocol'],
                realtime=dict(conversation=dict(pending_execution=['tracing']))),
            capacity=dict(max_seq=1), configuration=dict(original=True),
            startup_memory=dict(available=100), environment=dict(status='PASS'),
            business=dict(original=dict(model_quality='FAIL', answer='retained')),
            reused_business=dict(format='original-reviewed-record', failures=1))
        self.current = copy.deepcopy(self.prior)
        self.current['identity'].update(binary_sha256=REVIEW['candidate_binary'],
                                        protocol_sha256=REVIEW['candidate_protocol'])
        self.current['capabilities']['protocol_source_sha256'] = REVIEW['candidate_protocol']
        self.current['capabilities']['realtime']['conversation'].update(
            pending_execution=[], tracing=copy.deepcopy(evidence.CAPABILITY))
        self.current.update(format=profile_evidence.TRACING_FORMAT,
                            configuration=dict(current=True), startup_memory=dict(available=200))
        self.current['business']['realtime_tracing'] = dict(profile='primary')
        source = dict(record_sha256=hashlib.sha256(ledger.canonical(self.prior)+b'\n').hexdigest())
        observed = {k+'_sha256':ledger.digest(self.current[k]) for k in
                    ('configuration','startup_memory','environment','capabilities')}
        for patch in (mock.patch.object(reuse,'SOURCES',dict(primary=source)),
                      mock.patch.object(reuse,'OBSERVED',dict(primary=observed)),
                      mock.patch.object(evidence,'business'),
                      mock.patch.object(profile_mcp_reuse,'validate')):
            patch.start();self.addCleanup(patch.stop)

    def test_reconstruction_preserves_whole_original_record_and_failures(self):
        value = reuse.attach(self.current,self.prior)
        self.assertEqual(reuse.previous_record(value),self.prior)
        self.assertEqual(value['business']['original']['model_quality'],'FAIL')
        profile_mcp_reuse.validate.assert_called_once_with(self.prior)

    def test_old_answer_backend_capacity_and_metadata_mutations_are_rejected(self):
        for field in ('answer','backend','capacity','metadata'):
            value = reuse.attach(self.current,self.prior)
            if field=='answer': value['business']['original']['answer']='rewritten'
            elif field=='backend': value['identity']['backend']='changed'
            elif field=='capacity': value['capacity']['max_seq']=2
            else:
                meta=codec.unpack(value['reused_business']['previous'])
                meta['reused_business']['failures']=0
                value['reused_business']['previous']=codec.pack(meta)
            with self.assertRaises(ValueError):reuse.validate(value)

    def test_unknown_candidate_scope_or_review_cannot_reuse_prior_work(self):
        for field in ('binary','scope','review'):
            value = reuse.attach(self.current,self.prior)
            if field=='binary':value['identity']['binary_sha256']='0'*64
            elif field=='scope':value['profile']='light'
            else:value['reused_business']['review_sha256']='0'*64
            with self.assertRaises(ValueError):reuse.validate(value)

    def test_new_observations_must_be_from_the_actual_candidate(self):
        for field in ('configuration','startup_memory','environment'):
            value = reuse.attach(self.current,self.prior);value[field]['altered']=True
            with self.assertRaises(ValueError):reuse.validate(value)


if __name__=='__main__':unittest.main()
