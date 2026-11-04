"""Validate the actual retained gateway response without sending HTTP."""
import copy
import unittest
from unittest.mock import patch
from tools.serve_operations_listener import business_fix as f

class Repair(unittest.TestCase):
    def test_actual_401_is_gateway_contract_not_runner_schema(self):
        f.prior_gate()
        body=f.cpu.read(f.FAILED/'business/bad-key-wire.bin')
        headers=f.cpu.read(f.FAILED/'business/bad-key-http.json')
        f.gateway_error(body,headers,401,'gateway_unauthorized')
        for field,value in (('type','server_error'),('param','token'),('code','invalid_request')):
            wrong=copy.deepcopy(body);wrong['error'][field]=value
            with self.assertRaises(RuntimeError):f.gateway_error(wrong,headers,401,'gateway_unauthorized')

    def test_gateway_status_and_delivery_headers_are_required(self):
        body=f.cpu.read(f.FAILED/'business/bad-key-wire.bin')
        headers=f.cpu.read(f.FAILED/'business/bad-key-http.json')
        with self.assertRaises(RuntimeError):f.gateway_error(body,dict(headers,status=200),401,'gateway_unauthorized')
        for key in list(headers['headers']):
            if key.lower() in ('content-type','cache-control','www-authenticate','x-q4t-gateway-request-id'):
                wrong=copy.deepcopy(headers);del wrong['headers'][key]
                with self.assertRaises(RuntimeError):f.gateway_error(body,wrong,401,'gateway_unauthorized')

    def test_native_and_completed_controls_are_explicitly_reused(self):
        self.assertEqual(f.PLANNED['native_processes'],0)
        self.assertEqual(f.PLANNED['negative_probes']+f.PLANNED['reused_negative_probes'],6)
        self.assertEqual(f.PLANNED['successes']+f.PLANNED['cancelled'],f.s.LIMITS['generation_attempts'])

if __name__=='__main__':unittest.main()
