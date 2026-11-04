"""Regression checks for the real route mismatch and failure-before-join."""
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from tools.serve_gateway import gateway as g, acceptance as model
from tools.serve_gateway.test_gateway import GatewayTests


class CorrectionTests(unittest.TestCase):
    def test_routes_checked_against_formal_runner_api(self):
        self.assertEqual(g.validate_routes(), g.sha(g.API_CONTRACT))
        cases = []
        wrong = dict(g.ROUTES)
        wrong['/v1/chat/cancel'] = wrong.pop('/v1/requests/cancel')
        cases.append(wrong)
        for replacement in (('GET', 'control'), ('POST', 'data')):
            wrong = dict(g.ROUTES)
            wrong['/v1/requests/cancel'] = replacement
            cases.append(wrong)
        wrong = dict(g.ROUTES);del wrong['/v1/models'];cases.append(wrong)
        wrong = dict(g.ROUTES);wrong['/metrics'] = ('GET', 'control');cases.append(wrong)
        for routes in cases:
            with self.subTest(routes=tuple(routes)), mock.patch.object(g, 'ROUTES', routes):
                with self.assertRaises(ValueError):g.validate_routes()

    def test_failed_control_stops_backend_before_join(self):
        runner = object.__new__(model.Run)
        runner.cleaning = False
        entered, released = threading.Event(), threading.Event()
        completed = threading.Event()
        def request():
            entered.set()
            if not released.wait(2):raise AssertionError('joined before owned stop')
            completed.set()
        def stop():
            self.assertTrue(entered.is_set())
            self.assertFalse(completed.is_set())
            released.set()
        original = ValueError('cancel HTTP failed')
        with mock.patch.object(runner, 'stop_backend', side_effect=stop) as stopped:
            with self.assertRaises(ValueError) as error:
                with runner.concurrent_requests() as pool:
                    future = pool.submit(request)
                    self.assertTrue(entered.wait(1))
                    raise original
            self.assertIs(error.exception, original)
            stopped.assert_called_once()
        future.result()
        self.assertTrue(runner.cleaning and completed.is_set())


class ContractPackageTests(GatewayTests):
    def test_contract_identity_refuses_drift(self):
        receipt = self.prepare()
        contract = copy.deepcopy(g.parse(g.read_file(g.API_CONTRACT)))
        contract['info']['description'] = 'CPU fixture contract drift'
        alternate = self.folder/'changed-openapi.json'
        alternate.write_text(json.dumps(contract))
        with mock.patch.object(g, 'API_CONTRACT', alternate):
            with self.assertRaisesRegex(ValueError, 'API contract changed'):
                g.check(receipt['package'], receipt['manifest_sha256'])


if __name__ == '__main__':unittest.main()
