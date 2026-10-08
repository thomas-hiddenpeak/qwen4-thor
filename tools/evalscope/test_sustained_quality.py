"""Pure host tests; no tokenizer, HTTP, server, model, or generated code runs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sustained_quality import (BUDGET, CAPACITY, CASE_IDS, LENGTHS, SAMPLING,
                               PairBudget, analyze_responses, compare_pair,
                               excerpt_expected, file_sha256, join_expected,
                               ledger_expected, questionnaire, semantic_score,
                               text_sha256, validate_inputs, validate_reference,
                               write_json)


class SustainedQualityTest(unittest.TestCase):
    def setUp(self):
        self.frozen = questionnaire()
        self.manifest = [{key: case[key] for key in
                          ['id', 'language', 'category', 'length']}
                         for case in self.frozen['cases']]
        for row in self.manifest:
            row['prompt_sha256'] = text_sha256(row['id'])
        self.rows = [{'success': 1, 'response_id_valid': True,
                      'response_id': f'chatcmpl-{i}', 'actual_input': length,
                      'actual_output': 128, 'text': 'Observed text\n',
                      'finish': ['stop'], 'request_stream': True,
                      'decode_mode': 'plain', 'prompt_sha256': item['prompt_sha256']}
                     for i, (length, item) in enumerate(zip(LENGTHS, self.manifest))]

    def case(self, key):
        return next(c for c in self.frozen['cases'] if c['id'] == key)

    def pair(self):
        plain = analyze_responses(self.rows, self.manifest)
        strict = copy.deepcopy(plain)
        for row in strict:
            row['decode_mode'] = 'mtp'
            row['response_id'] += '-different'
        return plain, strict

    def test_frozen_matrix_and_budget(self):
        self.assertEqual([c['id'] for c in self.frozen['cases']], CASE_IDS)
        self.assertEqual([c['length'] for c in self.frozen['cases']], LENGTHS)
        self.assertEqual(sum(c['language'] == 'zh' for c in self.frozen['cases']), 4)
        for category in ['reasoning', 'code', 'summary', 'excerpt']:
            self.assertEqual(sum(c['category'] == category for c in self.frozen['cases']), 2)
        self.assertEqual(BUDGET['generation_requests'] * 512, BUDGET['output_tokens'])

    def test_exact_pair_ids_are_not_compared(self):
        report = compare_pair(*self.pair())
        self.assertTrue(report['exact_http_pair_passed'])
        self.assertTrue(report['sustained_coverage_passed'])
        self.assertFalse(report['compared_token_ids'])

    def test_whitespace_difference_fails_exact_pair(self):
        plain, strict = self.pair()
        strict[0]['text'] = strict[0]['text'].rstrip()
        self.assertFalse(compare_pair(plain, strict)['exact_http_pair_passed'])

    def test_prompt_usage_finish_each_bound(self):
        for field, value in [('prompt_sha256', 'different'), ('actual_input', 1023),
                             ('actual_output', 129), ('finish', ['length'])]:
            with self.subTest(field=field):
                plain, strict = self.pair()
                strict[0][field] = value
                self.assertFalse(compare_pair(plain, strict)['exact_http_pair_passed'])

    def test_mode_order_and_missing_cases_rejected(self):
        plain, strict = self.pair()
        with self.assertRaises(ValueError):
            compare_pair(plain, plain)
        with self.assertRaises(ValueError):
            compare_pair(plain, strict[:-1])
        strict.reverse()
        with self.assertRaises(ValueError):
            compare_pair(plain, strict)

    def test_early_eos_is_separate_missing_coverage(self):
        self.rows[0]['actual_output'] = 127
        plain = analyze_responses(self.rows, self.manifest)
        strict = copy.deepcopy(plain)
        for row in strict:
            row['decode_mode'] = 'mtp'
        report = compare_pair(plain, strict)
        self.assertTrue(report['exact_http_pair_passed'])
        self.assertFalse(report['sustained_coverage_passed'])
        self.assertTrue(report['cases'][0]['plain_early_eos'])
        self.assertEqual(len(report['cases']), 8)

    def test_exact_128_and_512_length_coverage(self):
        self.rows[1]['actual_output'] = 512
        self.rows[1]['finish'] = ['length']
        results = analyze_responses(self.rows, self.manifest)
        self.assertTrue(all(row['coverage_met'] for row in results))
        self.assertEqual(results[1]['derived_total_tokens'], LENGTHS[1] + 512)
        self.assertFalse(results[1]['token_ids_available'])

    def test_invalid_observations_rejected(self):
        for field, value in [('actual_output', True), ('actual_output', 0),
                             ('actual_output', 513), ('actual_input', 1023),
                             ('actual_input', True), ('actual_input', 1024.0),
                             ('request_stream', False), ('finish', ['length']),
                             ('finish', []), ('success', 0),
                             ('response_id_valid', False), ('prompt_sha256', 'bad')]:
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaises(ValueError):
                    analyze_responses(rows, self.manifest)

    def test_semantic_failure_does_not_mask_numerical_pass(self):
        plain, strict = self.pair()
        self.assertFalse(plain[0]['semantic']['automatic_rubric_passed'])
        self.assertTrue(compare_pair(plain, strict)['exact_http_pair_passed'])

    def test_ledger_oracle_derived_from_supplied_rows(self):
        case = self.case('zh-reasoning')
        expected = ledger_expected(case)
        included = [amount for _, label, amount in expected if label == '纳入']
        self.assertEqual(included, [120, 160, 190, 150, 110])
        text = '\n'.join(f'{key} {label} 金额={amount}；原因：依据正式记录判断。'
                         for key, label, amount in expected)
        text += f'\n合计：条数={len(included)}，金额={sum(included)}'
        self.assertTrue(semantic_score(case, text)['automatic_rubric_passed'])
        self.assertFalse(semantic_score(case, text.replace('730', '731'))['automatic_rubric_passed'])

    def test_join_oracle_uses_approved_revision_and_actual_chain(self):
        case = self.case('en-reasoning')
        expected = join_expected(case)
        self.assertEqual(expected[0][:5], ('amber', 'team1', 'north', 6, 'N66'))
        text = '\n'.join(f'{service} | {owner} | {zone} | {rev} | {code} | '
                         f'The newer revision {excluded[0]} is not approved.'
                         for service, owner, zone, rev, code, excluded in expected)
        self.assertTrue(semantic_score(case, text)['automatic_rubric_passed'])
        self.assertFalse(semantic_score(case, text.replace('N66', 'N99'))['automatic_rubric_passed'])

    def test_excerpt_is_exact_complete_source_not_plain_answer(self):
        for key in ['zh-excerpt', 'en-excerpt']:
            case = self.case(key)
            text = excerpt_expected(case)
            self.assertTrue(semantic_score(case, text)['automatic_rubric_passed'])
            self.assertFalse(semantic_score(case, text + '\n')['automatic_rubric_passed'])
            self.assertFalse(semantic_score(case, text[:-20])['automatic_rubric_passed'])

    def test_summary_checks_explicitly_need_manual_review(self):
        case = self.case('en-summary')
        anchors = ' '.join(case['rubric']['required'])
        qualifiers = ' '.join(choices[0] for choices in case['rubric']['alternatives'])
        text = f'1. {anchors}\n2. {qualifiers}\n3. Example\n4. Example\n5. Example\n6. Example'
        result = semantic_score(case, text)
        self.assertTrue(result['automatic_rubric_passed'])
        self.assertEqual(result['manual_review_status'], 'pending')
        self.assertTrue(result['manual_review_required'])
        self.assertFalse(semantic_score(case, text.replace('14:07', '14:08'))['automatic_rubric_passed'])

    def test_code_ast_is_not_functional_pass(self):
        text = '```python\ndef select_latest(rows):\n    return []\n'
        text += '\n'.join('assert select_latest([]) == []' for _ in range(6)) + '\n```'
        result = semantic_score(self.case('en-code'), text)
        self.assertTrue(result['automatic_rubric_passed'])
        self.assertFalse(result['generated_code_executed'])
        self.assertFalse(result['functionality_proven'])
        self.assertEqual(result['manual_review_status'], 'pending')

    def test_code_unsafe_or_unrequested_constructs_rejected_without_execution(self):
        for statement in ['import os', 'rows.append(1)', 'print(rows)',
                          'eval("1")', 'getattr(rows, "x")',
                          'return [x for x in rows]', '__builtins__',
                          '@decorator\ndef other():\n    pass']:
            text = ('```python\ndef select_latest(rows):\n    return []\n' +
                    statement + '\n' +
                    '\n'.join('assert select_latest([]) == []' for _ in range(6)) + '\n```')
            with self.subTest(statement=statement):
                self.assertFalse(semantic_score(self.case('en-code'), text)['automatic_rubric_passed'])

    def test_incomplete_code_is_preserved_as_failed_semantics(self):
        result = semantic_score(self.case('zh-code'), '```python\ndef approved_totals(')
        self.assertFalse(result['automatic_rubric_passed'])
        self.assertFalse(result['generated_code_executed'])

    def test_shared_budget_prevents_phase_retry_and_wrong_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = PairBudget(root, root / 'plain', {'binary': 'one'}, False)
            self.assertGreater(plain.check()['seconds_remaining'], 0)
            with self.assertRaises(FileExistsError):
                PairBudget(root, root / 'plain', {'binary': 'one'}, False)
            with self.assertRaises(ValueError):
                PairBudget(root, root / 'sequential', {'binary': 'two'}, True)
            strict = PairBudget(root, root / 'sequential', {'binary': 'one'}, True)
            self.assertLessEqual(strict.deadline, plain.deadline + 0.1)
            with self.assertRaises(FileExistsError):
                PairBudget(root, root / 'sequential', {'binary': 'one'}, True)

    def test_shared_deadline_and_byte_cap_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = PairBudget(root, root / 'plain', {}, False)
            with patch('sustained_quality.time.monotonic', return_value=budget.deadline + 1):
                with self.assertRaisesRegex(ValueError, 'deadline'):
                    budget.check()
            with patch('sustained_quality.BUDGET', {**BUDGET, 'evidence_bytes': 1}):
                with self.assertRaisesRegex(ValueError, 'evidence'):
                    budget.check()

    def test_output_must_belong_to_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'pair-root'):
                PairBudget(root, root / 'unrelated', {}, False)

    def test_reference_must_be_current_pair_plain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'this pair'):
                validate_reference(root / 'other/results.json', {}, root)

    def test_reference_reextracts_observations_and_requires_actual_mode_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / 'plain'
            (plain / 'inputs').mkdir(parents=True)
            (plain / 'sustained-quality').mkdir()
            from sustained_quality import QUESTIONNAIRE
            (plain / 'inputs/questionnaire.json').write_bytes(QUESTIONNAIRE.read_bytes())
            (plain / 'inputs/requests.jsonl').write_text('host fixture')
            write_json(plain / 'inputs/manifest.json', self.manifest)
            binding = {'binary_sha256': 'candidate',
                       'manifest_sha256': file_sha256(plain / 'inputs/manifest.json'),
                       'requests_sha256': file_sha256(plain / 'inputs/requests.jsonl'),
                       'questionnaire_sha256': file_sha256(plain / 'inputs/questionnaire.json')}
            write_json(plain / 'sustained-identity.json',
                       {'binding': binding, 'decode_mode': 'plain', 'verifier': 'none'})
            write_json(plain / 'run-mode.json',
                       {'acceptance_mode': 'sustained-quality', 'decode_mode': 'plain',
                        'verifier': 'none', 'capacity': CAPACITY})
            (plain / 'binary.sha256').write_text('candidate')
            write_json(plain / 'exit.json',
                       {'server': 0, 'completed': 8, 'actual_mode_checks_passed': True,
                        'http_output_checks_passed': True})
            write_json(plain / 'request-modes.json', {'passed': True})
            write_json(plain / 'sustained-quality/responses.json', self.rows)
            expected = analyze_responses(self.rows, self.manifest)
            write_json(plain / 'results.json', expected)
            self.assertEqual(validate_reference(plain / 'results.json', binding, root), expected)
            altered = copy.deepcopy(expected)
            altered[0]['text'] += ' changed after extraction'
            write_json(plain / 'results.json', altered)
            with self.assertRaisesRegex(ValueError, 'extraction'):
                validate_reference(plain / 'results.json', binding, root)
            write_json(plain / 'results.json', expected)
            write_json(plain / 'request-modes.json', {'passed': False})
            with self.assertRaisesRegex(ValueError, 'audit'):
                validate_reference(plain / 'results.json', binding, root)
            write_json(plain / 'request-modes.json', {'passed': True})
            (plain / 'binary.sha256').write_text('older binary')
            with self.assertRaisesRegex(ValueError, 'digest'):
                validate_reference(plain / 'results.json', binding, root)

    def test_fixture_binary_and_model_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'
            model.mkdir()
            for name in ['config.json', 'tokenizer_config.json', 'tokenizer.json']:
                (model / name).write_text('{}')
            binary = root / 'binary'
            binary.write_bytes(b'host fixture, not executable')
            inputs = root / 'inputs'
            inputs.mkdir()
            from sustained_quality import QUESTIONNAIRE, model_identity
            (inputs / 'questionnaire.json').write_bytes(QUESTIONNAIRE.read_bytes())
            manifest, requests = [], []
            for case in self.frozen['cases']:
                prompt = '\n'.join(b['text'] for b in case['blocks']) + '\n' + case['question']
                manifest.append({**{key: case[key] for key in
                                     ['id', 'language', 'category', 'length']},
                                 'prompt_sha256': text_sha256(prompt)})
                requests.append({'prompt': prompt, 'max_tokens': 512, 'temperature': 0,
                                 'seed': 20260920, 'stream': True,
                                 'stream_options': {'include_usage': True}})
            write_json(inputs / 'manifest.json', manifest)
            (inputs / 'requests.jsonl').write_text('\n'.join(json.dumps(r) for r in requests) + '\n')
            binding = {'binary_sha256': file_sha256(binary), 'model': model_identity(model),
                       'capacity': CAPACITY, 'sampling': SAMPLING,
                       'questionnaire_sha256': file_sha256(QUESTIONNAIRE),
                       'manifest_sha256': file_sha256(inputs / 'manifest.json'),
                       'requests_sha256': file_sha256(inputs / 'requests.jsonl')}
            write_json(inputs / 'identity.json', binding)
            self.assertEqual(validate_inputs(inputs, binary, model)[0], manifest)
            binary.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'binary'):
                validate_inputs(inputs, binary, model)
            binary.write_bytes(b'host fixture, not executable')
            (model / 'config.json').write_text('{"different": true}')
            with self.assertRaisesRegex(ValueError, 'model'):
                validate_inputs(inputs, binary, model)

    def test_production_admission_limits_matrix(self):
        from run_acceptance import admission_limit_cases
        targets = {length: Path(f'context-{length}/requests.jsonl')
                   for length in [1024, 208891, 208892]}
        cases = admission_limit_cases(Path('output'), targets)
        self.assertEqual(len(cases), 14)
        self.assertEqual([(c[3], c[5], c[6]) for c in cases[:12]],
                         [(1024, limit, streaming) for streaming in [True, False]
                          for limit in [1, 2, 3, 4, 5, 8]])
        self.assertEqual([(c[3], c[5], c[6]) for c in cases[-2:]],
                         [(208891, 8, True), (208892, 8, False)])
        self.assertEqual([min(c[5], 208896 - c[3]) for c in cases[-2:]], [5, 4])
        self.assertEqual(len({c[0].name for c in cases}), 14)

    def test_production_context_path_and_tail_reason(self):
        from run_acceptance import admission_limit_path_errors
        results = [{'id': 'context-five', 'response_id': 'five',
                    'expected_output': 5, 'length': 208891},
                   {'id': 'context-four', 'response_id': 'four',
                    'expected_output': 4, 'length': 208892}]
        paths = {'bindings': [
            {'response_id': 'five', 'records': [{'fields':
                {'path': 'mtp_sequential_b1', 'tail_reason': 'context_limit'}}]},
            {'response_id': 'four', 'records': [{'fields':
                {'path': 'plain_tail_b1', 'tail_reason': 'context_limit'}}]}]}
        self.assertEqual(admission_limit_path_errors(paths, results), [])
        paths['bindings'][0]['records'][0]['fields']['path'] = 'plain_tail_b1'
        self.assertEqual(len(admission_limit_path_errors(paths, results)), 1)
        paths['bindings'][1]['records'][0]['fields']['tail_reason'] = 'output_limit'
        self.assertEqual(len(admission_limit_path_errors(paths, results)), 2)


if __name__ == '__main__':
    unittest.main()
