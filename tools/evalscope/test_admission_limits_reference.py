"""Five pure-host contract groups; no model, HTTP, or database execution."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from acceptance_mode import request_mode_evidence, startup_evidence
from admission_limits_reference import (MATRIX, METADATA, OUTPUT_FAILURE,
                                        validate_admission_reference,
                                        validate_early_eos_option)
from sustained_quality import CAPACITY


class AdmissionLimitsReferenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / 'plain'
        self.prepared = self.root / 'new-inputs'
        self.binding = {'binary_sha256': 'a' * 64,
                        'model': {'metadata_sha256': {'config.json': 'b' * 64}},
                        'capacity': CAPACITY}

    def fixture(self, early_count=1):
        rows = []
        docs = {'admission-identity.json': copy.deepcopy(self.binding),
                'binary.sha256': self.binding['binary_sha256'],
                'run-mode.json': {'acceptance_mode': 'admission-limits',
                                  'decode_mode': 'plain', 'verifier': 'none',
                                  'capacity': CAPACITY},
                'exit.json': {'server': 0, 'completed': 14, 'decode_mode': 'plain',
                              'actual_mode_checks_passed': True,
                              'http_output_checks_passed': early_count is None,
                              'failure': None if early_count is None else OUTPUT_FAILURE}}
        log = ['[q4t][capabilities] effective mtp=0 media_allowed=0 '
               'vision_loaded=0 max_seq=1 verifier=none',
               '[q4t][capacity] requested_max_len=208896 requested_max_seq=1 '
               'requested_max_prefill=8192 effective_max_len=208896 '
               'effective_max_seq=1 effective_max_prefill=8192 '
               'budget_enabled=1 budget_feasible=true']
        for i, (case_id, length, limit, stream) in enumerate(MATRIX):
            prompt = f'Frozen prompt {length}\n'
            expected = min(limit, 208896 - length)
            terminal = i == 13 and early_count is not None
            count = early_count if terminal else expected
            row = {'success': 1, 'actual_input': length, 'actual_output': count,
                   'text': '' if terminal and count == 1 else 'Text\n',
                   'finish': ['stop'] if terminal else ['length'],
                   'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                   'ttft': .5, 'latency': 1., 'decode_mode': 'plain',
                   'request_stream': stream, 'response_id': f'chatcmpl-{i}',
                   'response_id_valid': True,
                   'response_ids_observed': [f'chatcmpl-{i}'],
                   'response_id_source': 'actual HTTP/SSE response id fields',
                   'id': case_id, 'streaming': stream, 'max_tokens': limit,
                   'length': length, 'expected_output': expected}
            rows.append(row)
            docs[case_id + '/responses.json'] = [
                {k: v for k, v in row.items() if k not in METADATA}]
            docs[case_id + '/requests.jsonl'] = {'prompt': prompt}
            docs[f'inputs/context-{length}/requests.jsonl'] = {'prompt': prompt}
            path = 'prefill_only' if count == 1 else 'plain'
            log.append(f'[q4t][decode_path] id=chatcmpl-{i} requested_mtp=0 '
                       f'path={path} mtp_steps=0 fallback=none '
                       'plain_tail_tokens=0 verifier=none')
        docs['results.json'] = rows
        docs['server.log'] = '\n'.join(log) + '\n'
        docs['startup-mode.json'] = startup_evidence(docs['server.log'], False)
        docs['request-modes.json'] = request_mode_evidence(docs['server.log'], rows, False)
        return docs

    def write(self, docs):
        for relative, value in docs.items():
            path = self.reference / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value if isinstance(value, str) else json.dumps(value),
                            encoding='utf-8')
        for length in (1024, 208891, 208892):
            path = self.prepared / f'context-{length}/requests.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'prompt': f'Frozen prompt {length}\n'}),
                            encoding='utf-8')

    def validate(self, enabled=True):
        return validate_admission_reference(self.reference / 'results.json',
                                             self.binding, self.prepared, enabled)

    def reject(self, edit):
        docs = self.fixture()
        edit(docs)
        self.write(docs)
        with self.assertRaises(ValueError):
            self.validate()

    def change_observation(self, docs, index, field, value):
        docs['results.json'][index][field] = value
        docs[MATRIX[index][0] + '/responses.json'][0][field] = value

    def test_explicit_flag_scope(self):
        valid = ('admission-limits', True, 'sequential', Path('results.json'))
        validate_early_eos_option(*valid, True)
        for mode, mtp, verifier, reference in [
                ('quality', True, 'sequential', Path('results.json')),
                ('sustained-quality', True, 'sequential', Path('results.json')),
                ('admission-limits', False, 'sequential', Path('results.json')),
                ('admission-limits', True, 't4', Path('results.json')),
                ('admission-limits', True, 'sequential', None)]:
            with self.subTest(mode=mode, mtp=mtp, verifier=verifier, reference=reference):
                with self.assertRaises(ValueError):
                    validate_early_eos_option(mode, mtp, verifier, reference, True)
                validate_early_eos_option(mode, mtp, verifier, reference, False)

    def test_default_rejects_eos_explicit_admits_without_rewriting_failure(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                docs = self.fixture(count)
                self.write(docs)
                before = {p: p.read_bytes() for p in self.reference.rglob('*') if p.is_file()}
                with self.assertRaises(ValueError):
                    self.validate(False)
                rows, report = self.validate()
                self.assertEqual(rows, docs['results.json'])
                self.assertFalse(report['reference_coverage_passed'])
                self.assertFalse(report['reference_http_output_checks_passed'])
                self.assertEqual(report['reference_failure'], OUTPUT_FAILURE)
                self.assertEqual(report['missing_coverage'], [MATRIX[-1][0]])
                self.assertTrue(report['strict_output_and_path_requirements_unchanged'])
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                for source in report['sources']:
                    data = Path(source['path']).read_bytes()
                    self.assertEqual(source['sha256'], hashlib.sha256(data).hexdigest())
                    self.assertEqual(source['bytes'], len(data))
        self.write(self.fixture(None))
        for enabled in (False, True):
            self.assertTrue(self.validate(enabled)[1]['reference_coverage_passed'])

    def test_only_final_legal_early_stop_may_lack_coverage(self):
        for index, field, value in [
                (13, 'actual_output', 0), (13, 'actual_output', 4),
                (13, 'actual_output', 5), (13, 'actual_output', True),
                (13, 'finish', ['length']), (13, 'finish', ['error']),
                (4, 'actual_output', 2), (4, 'finish', ['stop']),
                (13, 'expected_output', 1), (13, 'max_tokens', 4),
                (13, 'streaming', True)]:
            with self.subTest(index=index, field=field, value=value):
                if field in METADATA:
                    self.reject(lambda d: d['results.json'][index].update({field: value}))
                else:
                    self.reject(lambda d: self.change_observation(d, index, field, value))
        self.reject(lambda d: d['exit.json'].update(http_output_checks_passed=True,
                                                   failure=None))

    def test_identity_server_mode_order_and_path_failures_never_exempt(self):
        mutations = [
            lambda d: d['admission-identity.json'].update(binary_sha256='other'),
            lambda d: d.update({'binary.sha256': 'other'}),
            lambda d: d['run-mode.json'].update(decode_mode='mtp'),
            lambda d: d['run-mode.json'].update(verifier='sequential'),
            lambda d: d['run-mode.json'].update(capacity={}),
            lambda d: d['exit.json'].update(server=1),
            lambda d: d['exit.json'].update(completed=13),
            lambda d: d['exit.json'].update(actual_mode_checks_passed=False),
            lambda d: d['exit.json'].update(failure='RuntimeError: HTTP failed'),
            lambda d: d['results.json'].reverse(),
            lambda d: d.update({'results.json': d['results.json'][:-1]}),
            lambda d: d.update({'server.log': d['server.log'].replace('requested_mtp=0',
                                                                     'requested_mtp=1')}),
            lambda d: d['request-modes.json'].update(passed=False),
            lambda d: d['startup-mode.json'].update(passed=False),
        ]
        for index, edit in enumerate(mutations):
            with self.subTest(index=index):
                self.reject(edit)

    def test_complete_response_binding_and_http_observations(self):
        last = MATRIX[-1][0] + '/responses.json'
        mutations = [
            lambda d: d[last][0].update(text='changed'),
            lambda d: d['results.json'][-1].update(text='changed'),
            lambda d: d[last][0].pop('response_ids_observed'),
            lambda d: d[last].append(copy.deepcopy(d[last][0])),
            lambda d: d['results.json'][-1].update(extra='unbound'),
            lambda d: d[MATRIX[-1][0] + '/requests.jsonl'].update(prompt='changed'),
            lambda d: d['inputs/context-208892/requests.jsonl'].update(prompt='changed'),
        ]
        for index, edit in enumerate(mutations):
            with self.subTest(index=index):
                self.reject(edit)
        for field, value in [('success', 0), ('success', True),
                             ('actual_input', 208891), ('request_stream', True),
                             ('prompt_sha256', 'wrong'), ('response_id_valid', False),
                             ('response_ids_observed', ['chatcmpl-13', 'other']),
                             ('response_id', 'chatcmpl-0'),
                             ('response_id_source', 'invented'), ('text', None),
                             ('latency', -1), ('decode_mode', 'mtp')]:
            with self.subTest(field=field):
                self.reject(lambda d: self.change_observation(d, 13, field, value))


if __name__ == '__main__':
    unittest.main()
