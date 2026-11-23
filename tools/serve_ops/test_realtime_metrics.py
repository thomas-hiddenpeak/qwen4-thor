"""Archived real metrics and explicit privacy/resource-boundary counterexamples."""
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_ops import contracts as c, fixtures


class RealtimeMetrics(unittest.TestCase):
    def raw(self):
        return (Path(__file__).with_name('fixtures')/
                'native-v5-realtime-20261119.prom').read_text()

    def test_actual_archived_snapshot_projects_complete_realtime_group(self):
        values, omitted = c.parse_metrics(self.raw(), version=5)
        self.assertEqual(set(values), set(c.METRICS_V5) | set(c.METRICS_V5_ADDITIONS) |
                         set(c.METRICS_V5_REALTIME_ADDITIONS))
        self.assertEqual(omitted, 0)
        self.assertEqual(values['q4t_realtime_sessions'], 0)
        self.assertEqual(values['q4t_realtime_host_reserved_bytes'], 0)
        self.assertEqual(values['q4t_requests_total'], values['q4t_requests_success_total'])

    def test_old_v5_does_not_fabricate_realtime_zeroes(self):
        for raw in (fixtures.text_metrics(version=5),
                    (Path(__file__).with_name('fixtures')/'native-v5-20261116.prom').read_text()):
            values, _ = c.parse_metrics(raw, version=5)
            self.assertFalse(set(values) & set(c.METRICS_V5_REALTIME_ADDITIONS))

    def test_new_resource_group_cannot_be_partial_or_duplicated(self):
        raw = self.raw()
        for name in c.METRICS_V5_REALTIME_ADDITIONS:
            line = next(row+'\n' for row in raw.splitlines() if row.startswith(name+' '))
            for bad in (raw.replace(line, ''), raw+line):
                with self.assertRaises(c.InvalidObservation): c.parse_metrics(bad, version=5)

    def test_unknown_labels_and_malformed_values_still_reject(self):
        raw = self.raw()
        for name in c.METRICS_V5_REALTIME_ADDITIONS:
            line = next(row+'\n' for row in raw.splitlines() if row.startswith(name+' '))
            for value in ('NaN', '-1', '.5', str(2**64)):
                with self.assertRaises(c.InvalidObservation):
                    c.parse_metrics(raw.replace(line, name+' '+value+'\n'), version=5)
        for bad in (raw.replace('kind="realtime"', 'kind="unreviewed"'),
                    raw.replace('kind="realtime"', 'kind="realtime",secret="PRIVATE"')):
            with self.assertRaises(c.InvalidObservation): c.parse_metrics(bad, version=5)
        values, omitted = c.parse_metrics(raw+'unrelated{secret="PRIVATE"} 1\n', version=5)
        self.assertEqual(omitted, 1); self.assertNotIn('PRIVATE', json.dumps(values))

    def test_live_resource_values_and_uint64_counter_precision_preserved(self):
        raw = self.raw().replace('q4t_realtime_sessions 0\n', 'q4t_realtime_sessions 2\n')
        raw = raw.replace('q4t_realtime_host_reserved_bytes 0\n',
                          'q4t_realtime_host_reserved_bytes 2147483648\n')
        name = 'q4t_connection_rejections_total{kind="realtime"}'
        raw = raw.replace(name+' 0\n', name+' '+str(2**64-1)+'\n')
        values, _ = c.parse_metrics(raw, version=5)
        self.assertEqual(values[name], 2**64-1)
        self.assertEqual(values['q4t_realtime_sessions'], 2)
        self.assertEqual(values['q4t_realtime_host_reserved_bytes'], 2147483648)

    def test_all_native_connection_classes_are_in_the_fixed_projection(self):
        source = (fixtures.ROOT/'include/q4t/server/http_connection.h').read_text()
        declaration = source.split('kHttpConnectionClasses = {', 1)[1].split('};', 1)[0]
        names = re.findall(r'"([a-z_]+)"', declaration)
        self.assertTrue(names)
        contract = {**c.METRICS_V5, **c.METRICS_V5_ADDITIONS,
                    **c.METRICS_V5_REALTIME_ADDITIONS, **c.METRICS_V5_SIDEBAND_ADDITIONS}
        for name in names:
            for family in ('q4t_connection_rejections_total',
                           'q4t_operations_connection_rejections_total'):
                self.assertIn(c.key(family, kind=name), contract)

    def test_live_sideband_group_is_optional_and_complete(self):
        base = self.raw()
        rows = [f'{name} {i}\n' for i, name in enumerate(
            c.METRICS_V5_SIDEBAND_ADDITIONS, start=1)]
        values, _ = c.parse_metrics(base + ''.join(rows), version=5)
        for i, name in enumerate(c.METRICS_V5_SIDEBAND_ADDITIONS, start=1):
            self.assertEqual(values[name], i)
        for row in rows:
            incomplete = ''.join(r for r in rows if r != row)
            with self.assertRaises(c.InvalidObservation):
                c.parse_metrics(base + incomplete, version=5)
            with self.assertRaises(c.InvalidObservation):
                c.parse_metrics(base + ''.join(rows) + row, version=5)

    def test_failed_drain_keeps_original_response_before_parsing(self):
        from tools.native_asr import service_acceptance as service
        from tools.serve_ops import transport
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as tmp:
            stage = SimpleNamespace(output=Path(tmp), policy=dict(upstream_operations_port=1),
                                    budget=mock.Mock())
            response = dict(http_status=200, body='malformed observed metrics\n')
            with mock.patch.object(transport, 'get', return_value=response):
                with self.assertRaises(c.InvalidObservation): service.Stage.drained(stage, 'invalid')
            self.assertEqual(json.loads((Path(tmp)/'invalid.json').read_text()),
                             [dict(response=response)])


if __name__ == '__main__':
    unittest.main()
