"""Independent notification wire requirements and guard admission checks."""
import copy
import json
import os
import unittest
from unittest import mock

from tools.serve_notify import receiver, stage as s


def sample():
    return dict(version='4', receiver='q4t-webhook', status='firing',
        truncatedAlerts=0, alerts=[dict(status='firing', labels=dict(
            alertname='Q4TDeliveryProbe', deployment='thor-acceptance'),
            fingerprint='1234567890abcdef', startsAt='2026-11-04T00:00:00Z',
            endsAt='2026-11-04T00:04:00Z')])


class DriverTests(unittest.TestCase):
    def test_truncated_and_wrong_version_not_accepted_as_full_delivery(self):
        for key, value in [('truncatedAlerts', 1), ('version', '3'),
                           ('status', 'unknown'), ('receiver', 'other'), ('alerts', [])]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                receiver.payload(json.dumps(dict(sample(), **{key:value})).encode())

    def test_delivery_identity_and_real_resolved_status_required(self):
        value = sample()
        self.assertEqual(receiver.payload(json.dumps(value).encode()), value)
        value['status'] = value['alerts'][0]['status'] = 'resolved'
        self.assertEqual(receiver.payload(json.dumps(value).encode())['status'], 'resolved')
        for key, bad in [('fingerprint', ''), ('startsAt', ''), ('endsAt', ''),
                         ('labels', dict(deployment='other'))]:
            v = copy.deepcopy(value); v['alerts'][0][key] = bad
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                receiver.payload(json.dumps(v).encode())

    def test_wrong_or_released_guard_is_refused(self):
        good = dict(state='held', output=str(s.OUT/'lease'), pid=os.getppid())
        with mock.patch.object(s.a.guard, 'common_lock', return_value='fixture'), \
             mock.patch.object(s.n.pkg, 'read', return_value=good), \
             mock.patch.object(s.a, 'lock_available', return_value=False):
            s.require_guard()
            for key, value in [('state', 'released'), ('output', '/other'), ('pid', os.getpid())]:
                with mock.patch.object(s.n.pkg, 'read', return_value=dict(good, **{key:value})):
                    with self.assertRaises(RuntimeError): s.require_guard()
        with mock.patch.object(s.a.guard, 'common_lock', return_value='fixture'), \
             mock.patch.object(s.n.pkg, 'read', return_value=good), \
             mock.patch.object(s.a, 'lock_available', return_value=True):
            with self.assertRaises(RuntimeError): s.require_guard()

    def test_callback_rejection_and_acceptance_are_not_conflated(self):
        value = sample()
        instance = object.__new__(receiver.Receiver)
        import threading
        instance.lock = threading.RLock(); instance.errors = []
        instance.rows = [dict(response_status=503, payload=value)]
        self.assertFalse(instance.selected('Q4TDeliveryProbe', 'firing'))
        instance.rows.append(dict(response_status=204, payload=value))
        self.assertEqual(len(instance.selected('Q4TDeliveryProbe', 'firing')), 1)
        self.assertFalse(instance.selected('Q4TDeliveryProbe', 'resolved'))
        instance.errors.append('malformed body')
        with self.assertRaises(RuntimeError): instance.selected('Q4TDeliveryProbe', 'firing')


if __name__ == '__main__': unittest.main()
