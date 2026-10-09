"""Parser-only negative examples. Synthetic logs are NOT real T4 evidence."""
import unittest

import mtp_t4_completion_contract as fault


def synthetic_log():
    lines = [fault.VARIANT]
    for ordinal, (label, kind) in enumerate(fault.PLAN, 1):
        failed = kind != 'success'
        prefix = '[q4t][t4_fault_'
        base = f'ordinal={ordinal} real_ok=1 '
        lines += [prefix + 'begin] ' + base +
                  'slot=0 stage=prefill position=0 history=0 pending=0',
                  prefix + 'reset] ' + base + 'slot=0 completion=stream_ordered']
        if failed:
            lines += [prefix + 'injection] ' + base +
                      f'kind={kind} position=1024 history=1024 pending=0 step=1']
            if ordinal == 2:
                lines += [prefix + 'drain] ' + base +
                          'scope=verify after_injection=1 stream_match=1 thread_match=1',
                          prefix + 'verify_return] ' + base +
                          'status=failed inner_drained=1 checkpoint_rows=0 '
                          'checkpoint_slots=0 observer_cuda_calls=0']
            lines += [prefix + 'drain] ' + base +
                      'scope=step after_injection=1 stream_match=1 thread_match=1',
                      prefix + 'return] ' + base +
                      'status=failed counts=0 next_b=-1 next_d0=-1 '
                      'caller_unchanged=1 pending=0 observer_cuda_calls=0 '
                      f'inner_drains={int(ordinal == 2)} outer_drains=1 '
                      'next_g=invalid_not_read']
        steps = 0 if failed else 40
        path = 'plain' if failed else 'mtp_multi_b1'
        # A successful tail is legitimate in the combined terminal patch.
        tail = 0 if failed else 2
        lines += [f'[q4t][decode_path] id={label} requested_mtp=1 path={path} '
                  f'mtp_steps={steps} fallback=none plain_tail_tokens={tail} verifier=t4']
        if failed:
            lines += ['[q4t] generation failed id=' + label]
        lines += [prefix + 'end] ' + base +
                  f'injected={int(failed)} outputs_checked={int(failed)} '
                  f'failed_before_end={int(failed)} stage=idle position=0 '
                  f'history=0 pending=0 steps={1 if failed else 40} '
                  f'verifies={1 if failed else 40} '
                  f'restores={1 if ordinal == 6 else 0} '
                  f'extends={1 if ordinal == 8 else 0 if failed else 40} '
                  f'ordinary_calls={0 if failed else 1}']
    return '\n'.join(lines) + '\n'


class CompletionParserTest(unittest.TestCase):
    def reject(self, text):
        with self.assertRaises((AssertionError, RuntimeError, ValueError)):
            fault.validate_log(text)

    def test_synthetic_valid_shape(self):
        self.assertTrue(fault.validate_log(synthetic_log())['passed'])

    def test_missing_inner_completion(self):
        text = synthetic_log()
        text = '\n'.join(line for line in text.splitlines()
                         if not ('t4_fault_drain]' in line and
                                 'scope=verify' in line))
        self.reject(text)

    def test_outer_completion_cannot_replace_inner(self):
        self.reject(synthetic_log().replace('scope=verify', 'scope=step', 1))

    def test_completion_before_injection_does_not_count(self):
        lines = synthetic_log().splitlines()
        injection = next(i for i, line in enumerate(lines)
                         if 't4_fault_injection]' in line)
        lines[injection], lines[injection + 1] = lines[injection + 1], lines[injection]
        self.reject('\n'.join(lines))

    def test_wrong_stream(self):
        self.reject(synthetic_log().replace('stream_match=1', 'stream_match=0', 1))

    def test_wrong_thread(self):
        self.reject(synthetic_log().replace('thread_match=1', 'thread_match=0', 1))

    def test_stale_outputs(self):
        self.reject(synthetic_log().replace('counts=0', 'counts=313', 1))

    def test_checkpoint_still_published_on_upload_failure(self):
        self.reject(synthetic_log().replace('checkpoint_rows=0',
                                            'checkpoint_rows=3', 1))

    def test_test_side_wait_cannot_repair_production(self):
        self.reject(synthetic_log().replace('observer_cuda_calls=0',
                                            'observer_cuda_calls=1', 1))

    def test_failure_not_marked_failed(self):
        self.reject(synthetic_log().replace('failed_before_end=1',
                                            'failed_before_end=0', 1))

    def test_unrecognised_observer_event(self):
        self.reject(synthetic_log() + '[q4t][t4_fault_unknown] ordinal=9\n')

    def test_malformed_observer_event(self):
        self.reject(synthetic_log() + '[q4t][t4_fault_begin]ordinal=9\n')

    def test_failed_step_must_not_call_ordinary_decode(self):
        self.reject(synthetic_log().replace('ordinary_calls=0',
                                            'ordinary_calls=1', 1))

    def test_natural_restore_must_be_observed(self):
        self.reject(synthetic_log().replace('restores=1', 'restores=0', 1))

    def test_recovery_path_must_match_fresh(self):
        self.reject(synthetic_log().replace(
            'id=t4-upload-recovery requested_mtp=1 path=mtp_multi_b1 '
            'mtp_steps=40',
            'id=t4-upload-recovery requested_mtp=1 path=mtp_multi_b1 '
            'mtp_steps=39', 1))


if __name__ == '__main__':
    unittest.main()
