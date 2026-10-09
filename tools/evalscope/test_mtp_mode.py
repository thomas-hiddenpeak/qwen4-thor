"""Launcher mode contracts; no subprocess, server, model, or file writes."""
import unittest

from acceptance_mode import request_mode_evidence, startup_evidence
from mtp_mode import (cancellation_server_mode, resolve_new_run_verifier,
                      server_mtp_args)


def source_command(mtp=False, verifier=None):
    command = ['q4t', 'serve', '--port', '8000', '--max-seq', '1',
               '--max-len', '208896', '--max-prefill', '8192',
               '--mtp' if mtp else '--no-mtp']
    if verifier is not None:
        command += ['--mtp-verifier', verifier]
    return command


class NewRunModeTest(unittest.TestCase):
    def test_omitted_mtp_verifier_is_explicit_sequential(self):
        verifier = resolve_new_run_verifier(True)
        self.assertEqual(verifier, 'sequential')
        self.assertEqual(server_mtp_args(True, verifier),
                         ['--mtp', '--mtp-verifier', 'sequential'])

    def test_explicit_verifiers_are_preserved(self):
        for value in ('t4', 'sequential'):
            with self.subTest(value=value):
                verifier = resolve_new_run_verifier(True, value)
                self.assertEqual(verifier, value)
                self.assertEqual(server_mtp_args(True, verifier),
                                 ['--mtp', '--mtp-verifier', value])

    def test_plain_command_has_no_verifier(self):
        verifier = resolve_new_run_verifier(False)
        self.assertEqual(server_mtp_args(False, verifier), ['--no-mtp'])

    def test_plain_explicit_verifier_is_rejected(self):
        for value in ('t4', 'sequential'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_new_run_verifier(False, value)

    def test_invalid_modes_do_not_become_defaults(self):
        for value in ('', 'T4', 'fast', 'none'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_new_run_verifier(True, value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                server_mtp_args(True, value)
        with self.assertRaises(ValueError):
            server_mtp_args(False, 'sequential')


class CancellationModeTest(unittest.TestCase):
    def test_same_mode_default_ignores_source_verifier(self):
        for mtp, source in ((False, None), (True, None), (True, 't4'),
                            (True, 'sequential')):
            with self.subTest(mtp=mtp, source=source):
                original = source_command(mtp, source)
                command, verifier = cancellation_server_mode(
                    original, 'same-mode-recovery')
                self.assertEqual(verifier, 'sequential')
                self.assertEqual(command.count('--mtp'), 1)
                self.assertNotIn('--no-mtp', command)
                self.assertEqual(command.count('--mtp-verifier'), 1)
                self.assertEqual(command[command.index('--mtp-verifier') + 1],
                                 'sequential')
                self.assertEqual(original, source_command(mtp, source))

    def test_same_mode_explicit_t4_remains_t4(self):
        command, verifier = cancellation_server_mode(
            source_command(True, 'sequential'), 'same-mode-recovery', 't4')
        self.assertEqual(verifier, 't4')
        self.assertEqual(command[-3:], ['--mtp', '--mtp-verifier', 't4'])

    def test_same_mode_explicit_sequential_remains_sequential(self):
        command, verifier = cancellation_server_mode(
            source_command(True, 't4'), 'same-mode-recovery', 'sequential')
        self.assertEqual(verifier, 'sequential')
        self.assertEqual(command[-3:],
                         ['--mtp', '--mtp-verifier', 'sequential'])

    def test_decode_recovery_pins_historical_t4(self):
        original = source_command()
        command, verifier = cancellation_server_mode(original, 'decode-recovery')
        self.assertEqual(verifier, 't4')
        self.assertEqual(command[-3:], ['--mtp', '--mtp-verifier', 't4'])
        self.assertEqual(command[:-3], original[:-1])
        self.assertEqual(original, source_command())

    def test_decode_recovery_rejects_an_mtp_oracle(self):
        for value in (None, 't4', 'sequential'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cancellation_server_mode(source_command(True, value),
                                         'decode-recovery')

    def test_full_plain_remains_plain(self):
        original = source_command()
        command, verifier = cancellation_server_mode(original, 'full')
        self.assertEqual(command, original)
        self.assertEqual(verifier, 't4')  # Unused historical reader argument.
        self.assertNotIn('--mtp-verifier', command)

    def test_full_omitted_source_verifier_is_historical_t4(self):
        command, verifier = cancellation_server_mode(source_command(True), 'full')
        self.assertEqual(verifier, 't4')
        self.assertEqual(command[-3:], ['--mtp', '--mtp-verifier', 't4'])

    def test_full_explicit_source_verifier_is_not_reinterpreted(self):
        for value in ('t4', 'sequential'):
            with self.subTest(value=value):
                original = source_command(True, value)
                command, verifier = cancellation_server_mode(original, 'full')
                self.assertEqual(command, original)
                self.assertEqual(verifier, value)

    def test_scope_and_override_restrictions_remain(self):
        with self.assertRaises(ValueError):
            cancellation_server_mode(source_command(), 'unknown')
        for scope in ('full', 'decode-recovery'):
            for value in ('t4', 'sequential'):
                with self.subTest(scope=scope, value=value):
                    with self.assertRaises(ValueError):
                        cancellation_server_mode(source_command(), scope, value)
        with self.assertRaises(ValueError):
            cancellation_server_mode(source_command(), 'same-mode-recovery', '')

    def test_ambiguous_or_malformed_source_is_rejected(self):
        cases = [
            source_command() + ['--mtp'],
            source_command() + ['--no-mtp'],
            source_command()[:-1],
            source_command(True) + ['--mtp-verifier'],
            source_command(True, 't4') + ['--mtp-verifier', 'sequential'],
            source_command(True, 'invalid'),
            source_command(False, 't4'),
            source_command(True) + ['--mtp-verifier=t4'],
            source_command() + ['--mtp=1'],
        ]
        for command in cases:
            with self.subTest(command=command), self.assertRaises(ValueError):
                cancellation_server_mode(command, 'same-mode-recovery')


class HistoricalEvidenceModeTest(unittest.TestCase):
    def test_missing_startup_verifier_remains_t4(self):
        log = (
            '[q4t][capabilities] effective mtp=1 media_allowed=0 '
            'vision_loaded=0 max_seq=1\n'
            '[q4t][capacity] requested_max_len=208896 requested_max_seq=1 '
            'requested_max_prefill=8192 effective_max_len=208896 '
            'effective_max_seq=1 effective_max_prefill=8192 '
            'budget_enabled=1 budget_feasible=true\n')
        self.assertTrue(startup_evidence(log, True)['passed'])
        self.assertFalse(startup_evidence(
            log, True, resolve_new_run_verifier(True))['passed'])

    def test_missing_request_verifier_remains_t4(self):
        log = ('[q4t][decode_path] id=r1 requested_mtp=1 path=mtp_multi_b1 '
               'mtp_steps=2 fallback=none plain_tail_tokens=0\n')
        responses = [{'response_id': 'r1', 'actual_output': 8,
                      'finish': ['length']}]
        self.assertTrue(request_mode_evidence(log, responses, True)['passed'])
        self.assertFalse(request_mode_evidence(
            log, responses, True, resolve_new_run_verifier(True))['passed'])


if __name__ == '__main__':
    unittest.main()
