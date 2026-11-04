"""Verifier negative cases; no process, CUDA, model, or HTTP work."""
import copy
import json
import unittest

from tools.serve_cuda_quota import stage as s


def rows():
    mib = 1024*1024
    values = [dict(event='device', runtime=13030, driver=13020,
                   sm_major=11, sm_minor=0, integrated=1)]
    for value in (31,32,33,34):
        values.append(dict(event='verified', bytes=8*mib, value=value))
    for label, charged, entries, allocations, frees in (
            ('mixed_full',32,4,4,0), ('mixed_denied',32,4,4,0),
            ('mixed_released',0,0,4,4), ('mixed_recovered',0,0,5,5)):
        if label == 'mixed_recovered':
            values.append(dict(event='verified', bytes=32*mib, value=97))
        values.append(dict(event='quota', label=label, charged=charged*mib,
            entries=entries, allocations=allocations, frees=frees, syncs=0,
            pending=0, peak=32*mib, limit=32*mib, reserved=0, unknown=0,
            releasing=0, live=charged*mib, poisoned=False))
    values.append(dict(event='result', mode='mixed', passed=True))
    return values


def raw(values):
    return '\n'.join(json.dumps(value) for value in values)+'\n'


class Verifier(unittest.TestCase):
    def test_complete(self):
        result = s.parse_probe('mixed', raw(rows()))
        self.assertEqual(result['verified_bytes'], 64*1024*1024)
        self.assertEqual(result['verified_buffers'], 5)

    def test_missing_or_duplicated_records(self):
        original = rows()
        for values in (original[:-1], original+[original[-1]], original[1:],
                       original[:2]+original[3:], original[:5]+original[6:]):
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                s.parse_probe('mixed', raw(values))

    def test_bad_ownership_or_limit(self):
        for field, value in (('charged', 0), ('unknown', 1), ('poisoned', True),
                             ('live', 0), ('allocations', 5), ('entries', 3),
                             ('limit', 64*1024*1024), ('peak', 31*1024*1024)):
            values = rows()
            next(row for row in values if row['event'] == 'quota')[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                s.parse_probe('mixed', raw(values))

    def test_wrong_device_values_mode(self):
        for edit in ('device', 'value', 'mode', 'unexpected'):
            values = copy.deepcopy(rows())
            if edit == 'device': values[0]['sm_major'] = 9
            elif edit == 'value': values[1]['value'] = 0
            elif edit == 'mode': values[-1]['mode'] = 'async'
            else: values.insert(1, dict(event='unknown'))
            with self.subTest(edit=edit), self.assertRaises(RuntimeError):
                s.parse_probe('mixed', raw(values))


if __name__ == '__main__':
    unittest.main()
