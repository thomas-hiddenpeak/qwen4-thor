"""Reproduce the native gate rejecting a healthy custom-capacity model."""
import copy
import os
from pathlib import Path
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import binding,bundle,gateway,gateway_config,test_gateway,test_native_bundle
from tools.serve_service import capacity,supervisor


class GatewayCapacityTests(unittest.TestCase):
    def setUp(self):
        self.profile=capacity.normalize(dict(version=1,max_len=4096,max_prefill=1024,
            max_tokens=128,max_inflight_requests=6,max_queued_requests=4,request_deadline_ms=180000))
        self.config=test_gateway.config(Path(__file__).resolve().parents[2]/'build/capacity-gateway')
        self.config['capacity_profile']=self.profile

    def verify(self,config,change=None):
        snapshot=test_gateway.snapshot(config)
        expected=binding.validate(snapshot['binding'],base=config['backend_policy'],
            port=18182,operations_port=18183,binary='b'*64,protocol='c'*64)
        process=dict(executable_sha256='b'*64,listener_scope='loopback')
        cfg=dict(supervisor.expected_configuration(self.profile),default_output_tokens=128,
            request_deadline_ms=180000,mtp_k=3,startup_memory_headroom_mib=2048,operations_port=18183)
        if change:cfg.update(change)
        observed=dict(process=process,operations_process=process,protocol='c'*64,
            configuration=cfg,client_policy=dict(expected,profile='q4t-gateway-clients-v1',
                credential_values_redacted=True,mutable=False),operations_port=18183,
            observability_version=5,ready=True,health_status='healthy',metrics=None)
        channel=mock.Mock(service_id=str(uuid.uuid4()))
        admission=gateway.Admission(config,channel)
        try:
            with mock.patch.object(gateway.control,'binding',return_value=snapshot),\
                 mock.patch.object(supervisor,'probe',return_value=observed):
                # Real classifier and pidfd; only the model-free observation
                # transport is supplied by this CPU fixture.
                admission.update()
                return admission.cached is not None,channel
        finally:admission.close()

    def test_actual_classifier_accepts_selected_capacity_for_native_and_optional_nginx(self):
        for native in (False,True):
            cfg=copy.deepcopy(self.config)
            if native:
                cfg.update(version=2,native_ingress=cfg.pop('nginx'),
                           native_ingress_sha256=cfg.pop('nginx_sha256'))
            with self.subTest(native=native):
                ready,channel=self.verify(cfg)
                self.assertTrue(ready);channel.bound.assert_called_once_with(1)

    def test_missing_selected_profile_reproduces_failure_and_mismatch_still_refuses(self):
        old=copy.deepcopy(self.config);old.pop('capacity_profile')
        ready,channel=self.verify(old);self.assertFalse(ready);channel.bound.assert_not_called()
        for change in (dict(max_context_tokens=8192),dict(default_output_tokens=256),
                       dict(request_deadline_ms=1200000),dict(mtp_k=True)):
            with self.subTest(change=change):
                ready,channel=self.verify(self.config,change)
                self.assertFalse(ready);channel.bound.assert_not_called();channel.begin.assert_not_called()

    def test_profile_validation_does_not_allow_wrong_types_or_client_capacity(self):
        for change in (dict(max_len=True),dict(max_inflight_requests=3),dict(max_queued_requests=1)):
            cfg=copy.deepcopy(self.config);cfg['capacity_profile'].update(change)
            with self.assertRaises((ValueError,RuntimeError)):gateway_config.validate(cfg)

    def test_bundle_projects_verified_engine_capacity_into_independent_admission(self):
        fixture=test_native_bundle.BundleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.base.update(version=11)
        fixture.base['settings']['profile']=self.profile
        receipt=fixture.prepare(capacity_config=dict(version=1,max_len=4096,max_prefill=1024,
            max_tokens=128,max_inflight_requests=6,max_queued_requests=4,request_deadline_ms=180000))
        manifest=bundle.verify(receipt['package'],receipt['manifest_sha256'])
        cfg=bundle.gateway_config_for(receipt['package'],manifest,fixture.directory)
        self.assertEqual(cfg['capacity_profile'],self.profile)


if __name__=='__main__':unittest.main()
