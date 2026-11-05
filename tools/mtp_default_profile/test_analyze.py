"""Reject timing records that cannot be bound to completed HTTP requests."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'default_profile_analysis', Path(__file__).with_name('analyze.py'))
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def fixture():
    lines = []
    for request in ('one', 'two'):
        lines += [
            f'[q4t][request] id={request} event=admitted seq=0',
            '[mtp-timing] B=1 draft=10.0 verify=80.0 extend+accept=5.0 '
            'ms (k=3 a=[3]) suffix=0 gpu[ draft=9.0 verify=79.0 extend=4.0 ]',
            '[layer-timing] T=4 embed=0.00ms lin=50.00ms full=20.00ms '
            'head=9.00ms total=79.00ms top5= L0(2.00)',
            f'[mtp-accept] request={request} draft_counts=0,0,0,1',
            f'[q4t][request] id={request} event=end released=1 outcome=success',
        ]
    return '\n'.join(lines)


class RequestBindingTests(unittest.TestCase):
    def test_host_and_event_clocks_stay_separate(self):
        row = analysis.request_logs(fixture())['one']
        self.assertEqual(row['mtp'][0]['host']['verify'], 80)
        self.assertEqual(row['mtp'][0]['event']['verify'], 79)
        self.assertEqual(row['histogram'], [0, 0, 0, 1])

    def test_missing_event_clock_rejected(self):
        raw = fixture().replace(' gpu[ draft=9.0 verify=79.0 extend=4.0 ]', '')
        with self.assertRaises(ValueError):
            analysis.request_logs(raw)

    def test_acceptance_bound_to_http_identity(self):
        with self.assertRaises(ValueError):
            analysis.request_logs(fixture().replace('request=one', 'request=foreign'))

    def test_unclosed_request_rejected(self):
        raw = fixture().splitlines()
        with self.assertRaises(ValueError):
            analysis.request_logs('\n'.join(raw[:-1]))

    def test_unbound_timing_rejected(self):
        raw = fixture()
        with self.assertRaises(ValueError):
            analysis.request_logs(raw + '\n' + raw.splitlines()[1])

    def test_failed_event_report_rejected(self):
        raw = fixture().replace('T=4 embed=0.00ms', 'sync failed: bad resource')
        with self.assertRaises(ValueError):
            analysis.request_logs(raw)

    def test_summary_preserves_first_and_rounding(self):
        summary = analysis.summarize([{'x': 100}, {'x': 2}, {'x': 4}], ['x'], .005)
        self.assertEqual(summary['sum_ms']['x'], 106)
        self.assertEqual(summary['first_ms']['x'], 100)
        self.assertEqual(summary['subsequent_mean_ms']['x'], 3)
        self.assertEqual(summary['printed_sum_rounding_bound_ms'], .015)


if __name__ == '__main__':
    unittest.main()
