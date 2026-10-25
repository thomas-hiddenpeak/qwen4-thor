"""CPU-only synthetic contracts; no model, collector, or historical analysis."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    'q4t_pair_work_contract_tested', Path(__file__).with_name('pair_work_contract.py'))
work = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(work)


class PairWorkContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def fixture(self, name='nsys8-on'):
        enabled, inputs, capture = work.ARMS[name]
        step = dict(name=name, head='subset', suffix=enabled,
                    execution_diagnostic=True, measurement_role='nsys_profile',
                    profile_input_tokens=capture, http_loads=1,
                    http_requests=len(inputs), cases=[])
        lines, results = [], []
        if enabled:
            lines += ['[suffix-classes] event=init bytes=248320 cap_bytes=262144 unknown=243 init_ns=9',
                      '[q4t] suffix enabled: numeric_gate=1 min_admitted_match=32']
        batch_id = 0

        def batch(label, position, tokens, spans):
            nonlocal batch_id
            batch_id += 1
            lines.append(f'[exec-batch] pid=123 tid=124 batch={batch_id} label={label} '
                         f'seq=0 p={position} t={tokens} records={len(spans)} capacity=4096 '
                         'overflow=0 clock_error=0 complete=1')
            for index, (phase, p, t, parent) in enumerate(spans):
                lines.append(f'[exec-span] pid=123 tid=124 batch={batch_id} id={index} '
                             f'parent={parent} phase={phase} seq=0 p={p} t={t} '
                             'wall_begin_ns=100 wall_end_ns=200 cpu_begin_ns=100 '
                             'cpu_end_ns=150 closed=1')
            lines.append(f'[exec-flush] batch={batch_id} wall_ns=1 cpu_ns=1 clock_error=0')

        for ordinal, tokens in enumerate(inputs):
            oracle = work.ORACLES[tokens]
            key = f'chatcmpl-auto-{ordinal}'
            step['cases'].append(dict(id=oracle['case'], input_tokens=tokens,
                output_tokens=256, repeats=1, prompt_sha256=oracle['prompt'],
                expected_output_sha256=oracle['output']))
            results.append(dict(id=oracle['case'], outputs=[oracle['output']],
                responses=[dict(response_id=key, success=1, actual_input=tokens,
                                actual_output=256, finish=['length'],
                                prompt_sha256=oracle['prompt'])]))
            lines.append(f'[q4t][request] instance={ordinal} id={key} event=admitted seq=0')
            if tokens == capture:
                lines.append(f'[exec-capture] event=begin pid=123 tid=124 input_tokens={tokens} error=0')
            lines.append(f'[q4t][request] instance={ordinal} id={key} event=stage stage=prefill seq=0')
            chunks = [(p, min(8192, tokens - p)) for p in range(0, tokens, 8192)]
            spans = [('handler_prefill', 0, tokens, -1),
                     ('target_begin_sequence', 0, tokens, 0)]
            for p, t in chunks:
                parent = 0
                if tokens > 8192:
                    parent = len(spans)
                    spans.append(('target_prefill_chunk', p, t, 0))
                spans.append(('target_prefill_call', p, t, parent))
                if p + t < tokens:
                    spans.append(('prefill_chunk_readback_wait_commit', p, t, parent))
            for phase in ('prefill_final_d2h_submit', 'prefill_final_readback_wait_commit'):
                spans.append((phase, *chunks[-1], 0))
            batch(key, 0, tokens, spans)
            lines.append(f'[q4t][request] instance={ordinal} id={key} event=stage stage=decode seq=0')
            spans = [('handler_mtp_init', 0, tokens, -1),
                     ('prompt_draft_reset', 0, tokens, 0),
                     ('prompt_draft_extend_call', 0, tokens, 0),
                     ('mtp.prompt_extend.total', 0, tokens, 2)]
            for p, t in chunks:
                parent = 3
                if tokens > 8192:
                    parent = len(spans)
                    spans.append(('mtp.prompt_extend.chunk', p, t, 3))
                spans.append(('mtp.forward', p, t, parent))
            spans.append(('prompt_verify_reserve', 0, tokens, 0))
            batch(key, 0, tokens, spans)
            batch(key, 0, tokens, [('prompt_trunk_free', 0, tokens, -1)])
            p = tokens
            for accepted, count in enumerate(oracle['histogram']):
                for _ in range(count):
                    spans = [('mtp.step', p, 4, -1), ('mtp.draft_token', p, 1, 0),
                             ('mtp.forward', p, 1, 1),
                             ('mtp.draft_token', p + 1, 1, 0),
                             ('mtp.forward', p + 1, 1, 3),
                             ('mtp.verify_forward', p, 4, 0)]
                    if accepted < 3:
                        spans.append(('mtp.restore_checkpoint', p, 4, 0))
                    extend_index = len(spans)
                    spans.append(('mtp.accepted_extend', p, accepted + 1, 0))
                    spans.append(('mtp.forward', p + accepted, accepted + 1, extend_index))
                    batch('mtp.multi', p, 4, spans)
                    p += accepted + 1
            hist = ','.join(map(str, oracle['histogram']))
            lines.append(f'[mtp-accept] request={key} draft_counts={hist}')
            if enabled:
                short = oracle['short_rejected']
                lines.append(f'[suffix] request={key} queries=74 hits=0 '
                    f'no_match={74-short} incomplete=0 d0_mismatch=0 invalid=0 exhausted=0 '
                    'peak_bytes=1884160 query_ns=100 numeric_ns=5 '
                    f'numeric_checks={short} classified_tokens={6*short} numeric_rejected=0 '
                    f'short_rejected={short} accepted=0,0,0,0 neural_accepted={hist}')
            if tokens == capture:
                lines.append(f'[exec-capture] event=end pid=123 tid=124 input_tokens={tokens} error=0')
            lines.append(f'[q4t][request] instance={ordinal} id={key} event=end seq=0 '
                         f'released=1 outcome=success reason=completed scheduler_waits={74+oracle["tail"]}')
        if enabled:
            lines.append('[suffix-classes] event=release bytes=248320 release_ns=9')
        lines.append('[startup-reservation] ' + json.dumps(dict(
            event='finalized', status='PASS', release_unknown=0)))
        (self.output / 'server.log').write_text('\n'.join(lines) + '\n')
        (self.output / 'server-command.json').write_text(json.dumps(
            dict(argv=['q4t', 'serve'] + (['--mtp-suffix'] if enabled else []))))
        (self.output / 'exit.json').write_text(json.dumps(dict(server=0, http_output_checks_passed=True)))
        (self.output / 'results.json').write_text(json.dumps(results))
        return step

    def change(self, old, new, count=1):
        path = self.output / 'server.log'
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, count))

    def invalid(self, step, contains=None):
        result = work.validate_work(self.output, step, 123)
        self.assertEqual(result['status'], 'INVALID', result)
        self.assertTrue(result['errors'])
        if contains:
            self.assertIn(contains, ' '.join(result['errors']))

    def test_four_fixed_arms_and_long_predecessor(self):
        for name in work.ARMS:
            with self.subTest(name=name):
                step = self.fixture(name)
                work.audit_server(self.output, step['http_requests'])
                result = work.validate_work(self.output, step, 123)
                self.assertEqual(result['status'], 'VALID', result['errors'])
                self.assertEqual(len(result['requests']), step['http_requests'])
                self.assertEqual(result['counters']['verify_T4'], 74)
                self.assertEqual(result['counters']['neural_draft_T1'], 148)
                self.assertEqual(result['counters']['tail_T1'], 2 if '200' in name else 3)
                self.assertEqual(len(result['counters']['target_prefill_chunks']),
                                 25 if '200' in name else 1)
                if '200' in name:
                    self.assertEqual(result['capture_request_id'], 'chatcmpl-auto-1')
                    self.assertEqual(result['counters']['prompt_draft_chunks'][-1], (196608, 8173))

    def test_only_request_prefix_controls_lifecycle(self):
        step = self.fixture()
        with (self.output / 'server.log').open('a') as stream:
            stream.write('[foreign] event=admitted id=not-a-request\n'
                         '[foreign] event=end id=also-not-a-request\n')
        work.audit_server(self.output, 1)
        audit = json.loads((self.output / 'server-audit.json').read_text())
        self.assertEqual(audit['admitted_requests'], 1)
        self.assertEqual(work.validate_work(self.output, step, 123)['status'], 'VALID')

    def test_lifecycle_errors_remain_fatal(self):
        changes = [('released=1', 'released=0'),
                   ('event=end seq=0', 'event=end seq=1'),
                   ('event=end seq=0', 'event=missing seq=0'),
                   ('instance=0 id=chatcmpl-auto-0 event=end',
                    'instance=9 id=chatcmpl-auto-0 event=end')]
        for old, new in changes:
            with self.subTest(old=old):
                self.fixture(); self.change(old, new)
                with self.assertRaises(RuntimeError):
                    work.audit_server(self.output, 1)

    def test_table_and_suffix_accounting_fail_closed(self):
        changes = [('cap_bytes=262144', 'cap_bytes=2'), ('unknown=243', 'unknown=0'),
                   ('min_admitted_match=32', 'min_admitted_match=31'),
                   ('event=release bytes=248320', 'event=lost bytes=248320'),
                   ('numeric_ns=5', 'numeric_ns=101'), ('numeric_checks=1', 'numeric_checks=2'),
                   ('invalid=0', 'invalid=1'), ('exhausted=0', 'exhausted=1'),
                   ('short_rejected=1', 'short_rejected=0')]
        for old, new in changes:
            with self.subTest(old=old):
                self.fixture(); self.change(old, new)
                with self.assertRaises(RuntimeError):
                    work.audit_server(self.output, 1)
        self.fixture('nsys8-off')
        with (self.output / 'server.log').open('a') as stream:
            stream.write('[suffix-classes] event=init bytes=248320\n')
        with self.assertRaises(RuntimeError):
            work.audit_server(self.output, 1)

    def test_capture_identity_and_record_closure(self):
        changes = [('event=begin pid=123', 'event=begin pid=999'),
                   ('event=end pid=123 tid=124', 'event=end pid=123 tid=125'),
                   ('event=begin pid=123 tid=124 input_tokens=8170',
                    'event=begin pid=123 tid=124 input_tokens=204781'),
                   ('overflow=0', 'overflow=1'), ('closed=1', 'closed=0'),
                   ('[exec-flush] batch=1', '[exec-unknown] batch=1'),
                   ('wall_end_ns=200', 'wall_end_ns=99')]
        for old, new in changes:
            with self.subTest(old=old):
                step = self.fixture(); self.change(old, new); self.invalid(step)
        step = self.fixture('nsys200-on')
        self.change('event=begin pid=123 tid=124 input_tokens=204781',
                    'event=begin pid=123 tid=124 input_tokens=8170')
        self.invalid(step)

    def test_generation_work_and_long_chunks(self):
        changes = [('phase=mtp.verify_forward seq=0 p=8170 t=4',
                    'phase=mtp.verify_forward seq=0 p=8170 t=3'),
                   ('phase=mtp.draft_token', 'phase=missing_draft'),
                   ('phase=mtp.restore_checkpoint', 'phase=missing_restore'),
                   ('phase=mtp.accepted_extend seq=0 p=8170 t=1',
                    'phase=mtp.accepted_extend seq=0 p=8170 t=2'),
                   ('draft_counts=7,8,7,52', 'draft_counts=8,7,7,52'),
                   ('scheduler_waits=77', 'scheduler_waits=76')]
        for old, new in changes:
            with self.subTest(old=old):
                step = self.fixture(); self.change(old, new); self.invalid(step)
        for name in ('target_prefill_call', 'mtp.prompt_extend.chunk', 'mtp.forward'):
            with self.subTest(long_phase=name):
                step = self.fixture('nsys200-off')
                self.change(f'phase={name} seq=0 p=196608 t=8173',
                            f'phase={name} seq=0 p=196608 t=8172')
                self.invalid(step)

    def test_http_and_frozen_step_identity(self):
        for field, replacement in [('actual_input', 8192), ('actual_output', 255),
                                   ('finish', ['stop']), ('response_id', 'foreign')]:
            with self.subTest(field=field):
                step = self.fixture()
                path = self.output / 'results.json'; value = json.loads(path.read_text())
                value[0]['responses'][0][field] = replacement
                path.write_text(json.dumps(value)); self.invalid(step)
        step = self.fixture(); broken = copy.deepcopy(step); broken['suffix'] = False
        self.invalid(broken)
        step = self.fixture('nsys200-on'); step['cases'].reverse(); self.invalid(step)
        step = self.fixture(); path = self.output / 'results.json'
        data = json.loads(path.read_text()); data[0]['outputs'] = ['0' * 64]
        path.write_text(json.dumps(data)); self.invalid(step)

    def test_fallback_exit_and_finalization(self):
        for text in ('MTP init failed', 'MTP trunk alloc failed', 'retained after failed drain'):
            with self.subTest(text=text):
                self.fixture()
                with (self.output / 'server.log').open('a') as stream:
                    stream.write(text + '\n')
                with self.assertRaises(RuntimeError): work.audit_server(self.output, 1)
        self.fixture(); self.change('"status": "PASS"', '"status": "FAIL"')
        with self.assertRaises(RuntimeError): work.audit_server(self.output, 1)
        self.fixture(); (self.output / 'exit.json').write_text(
            json.dumps(dict(server=1, http_output_checks_passed=True)))
        with self.assertRaises(RuntimeError): work.audit_server(self.output, 1)

    def test_missing_malformed_and_bounded_sources(self):
        step = self.fixture(); (self.output / 'results.json').write_text('{')
        self.invalid(step)
        step = self.fixture(); (self.output / 'exit.json').unlink(); self.invalid(step)
        step = self.fixture(); self.change('cpu_end_ns=150', 'cpu_end_ns=unknown')
        self.invalid(step)
        step = self.fixture()
        with self.assertRaisesRegex(ValueError, 'oversized'):
            work.read(self.output / 'server.log', limit=8)


if __name__ == '__main__':
    unittest.main()
