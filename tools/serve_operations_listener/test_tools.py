"""CPU boundary checks; transport and listener observations are substituted."""
import copy
import json
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch

from tools.serve_ops import contracts as c, diagnose, fixtures as f, identity, transport
from tools.serve_ops.test_ops import Files, instance
from tools.serve_service import package as pkg, supervisor as sup, service
from tools.serve_service.test_service import PackageTests
from tools.serve_gateway import gateway as g, managed as m, client_policy as cp
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_clients.test_packages import policy as client_policy


class Contracts(unittest.TestCase):
    def test_versioned_metrics_never_fall_back_on_missing_operations_series(self):
        for version in (3,4,5):
            observed, _ = c.parse_metrics(f.text_metrics(version=version),version=version)
            self.assertEqual(set(observed),set(c.METRIC_PROFILES[version]))
        with self.assertRaises(c.InvalidObservation):
            c.parse_metrics(f.text_metrics(version=4),version=5)

    def test_operations_limits_and_disabled_owners(self):
        for name, value in (('operations_connections',9),('operations_observation_connections',5),
                            ('operations_control_connections',5),('operations_listener_enabled',2),
                            ('operations_listener_enabled',0)):
            values=f.metrics(version=5); values['q4t_'+name]=value
            with self.subTest(name=name), self.assertRaises(c.InvalidObservation):
                c.parse_metrics(f.text_metrics(values),version=5)
        values=f.metrics(version=5)
        for name in ('operations_connections','operations_observation_connections','operations_listener_enabled'):
            values['q4t_'+name]=0
        c.parse_metrics(f.text_metrics(values),version=5)

    def test_config_requires_matching_listener_capacity_and_version(self):
        value=f.configuration(version=5)
        self.assertEqual(c.configuration(value,version=5)['operations_port'],18108)
        variants=[]
        for group, name, replacement in (('operations','port',18107),('operations','enabled',False),
            ('operations','host','0.0.0.0'),('operations','port',True),('public','port',0)):
            bad=copy.deepcopy(value);bad['listeners'][group][name]=replacement;variants.append(bad)
        for name, replacement in (('connection_handlers',136),('operations_connection_handlers',7),
            ('public_connection_handlers',144),('unclassified_shares_total',True)):
            bad=copy.deepcopy(value);bad['limits'][name]=replacement;variants.append(bad)
        bad=copy.deepcopy(value);bad['observability_version']=4;variants.append(bad)
        for bad in variants:
            with self.assertRaises(c.InvalidObservation):c.configuration(bad,version=5)
        value['listeners']['operations'].update(enabled=False,port=0)
        value['limits'].update(operations_connection_handlers=0,
            connection_handlers=value['limits']['public_connection_handlers'],unclassified_shares_total=True)
        c.configuration(value,version=5)

    def test_distinct_loopback_socket_must_have_same_owner_elf_and_namespace(self):
        primary=instance();ops=dict(primary,listener_inode=502)
        with patch.object(identity,'process',return_value=ops):
            self.assertEqual(identity.operations(primary,18108),ops)
        for key, value in (('pid',12346),('start_ticks',12001),('boot_id','different'),
            ('executable_sha256','b'*64),('net_namespace_inode',3),('listener_scope','wildcard'),
            ('listener_inode',501)):
            with patch.object(identity,'process',return_value=dict(ops,**{key:value})),self.assertRaises(c.InvalidObservation):
                identity.operations(primary,18108)

    def test_credentials_cannot_escape_discovery_or_inject_headers(self):
        valid=dict(client='alpha',token='a'*64)
        with patch.object(transport.socket,'create_connection') as connect:
            for path, credentials in (('/metrics',valid),('/readyz',valid),
                ('/v1/capabilities',dict(valid,token='a'*64+'\r\nHost: bad')),
                ('/v1/capabilities',dict(valid,client='a\r\n'))):
                with self.assertRaises(c.InvalidObservation):transport.get(18108,path,credentials=credentials)
            connect.assert_not_called()


class Diagnostic(Files):
    def collect(self, *, owner_change=False, wrong_config=False, wrong_policy=False, use_policy=True):
        p=client_policy();raw=json.dumps(p).encode();path=self.root/'clients.json'
        path.write_bytes(raw);path.chmod(0o600)
        calls=[];primary=instance();ops=dict(primary,listener_inode=502)
        config=f.configuration(version=5)
        config['client_policy']=dict(cp.binding(raw),profile='q4t-gateway-clients-v1',
            mutable=False,credential_values_redacted=True)
        if wrong_policy:config['client_policy']['sha256']='f'*64
        if wrong_config:config['listeners']['operations']['port']=18109
        def get(port,route,**kwargs):
            calls.append((port,route,copy.deepcopy(kwargs)))
            self.assertEqual(port,18108)
            if route=='/v1/capabilities':
                if use_policy:self.assertEqual(kwargs['credentials'],dict(client='alpha',token=p['gateway_token']))
                else:return dict(http_status=401,content_type='application/json',body='{}')
            else:self.assertNotIn('credentials',kwargs)
            reply=f.reply(18107,route,version=5)
            if route=='/v1/config':reply['body']=json.dumps(config)
            return reply
        with patch.object(identity,'release_identity',return_value=dict(binary_sha256='a'*64,
                protocol_sha256=config['protocol_source_sha256'])), \
             patch.object(identity,'process',return_value=primary), \
             patch.object(identity,'operations',side_effect=([ops,ops,dict(ops,listener_inode=503) if owner_change else ops]
                          if use_policy else [ops,ops])), \
             patch.object(transport,'get',side_effect=get):
            report=diagnose.snapshot(18107,12345,self.root/'release',operations_port=18108,
                client_policy_path=path if use_policy else None)
        self.assertEqual(len(calls),5)
        self.assertNotIn(p['gateway_token'],json.dumps(report))
        return report

    def test_authenticated_local_diagnostic_uses_only_operations_listener(self):
        report=self.collect()
        self.assertEqual(report['state'],'HEALTHY')
        self.assertEqual(report['identity']['state'],'verified')
        self.assertEqual(report['observability_version'],5)

    def test_rebound_listener_config_policy_and_anonymous_discovery_are_not_healthy(self):
        for key in ('owner_change','wrong_config','wrong_policy','use_policy'):
            report=self.collect(**{key:key!='use_policy'})
            self.assertEqual(report['state'],'UNKNOWN')
            self.assertNotEqual(report['identity']['state'],'verified')

    def test_interval_does_not_join_two_operations_listener_identities(self):
        old=self.collect();new=copy.deepcopy(old);new['monotonic']+=1
        new['identity']['operations_process']['listener_inode']+=1
        self.assertEqual(diagnose.compare(old,new)['state'],'unavailable')

    def test_private_credential_never_sent_to_unverified_or_replaced_owner(self):
        policy=client_policy();path=self.root/'private.json'
        path.write_text(json.dumps(policy));path.chmod(0o600)
        owner=instance();ops=dict(owner,listener_inode=502)
        for reason in ('wrong_elf','unknown_operations','replaced_before_auth'):
            sequence=([c.InvalidObservation('listener_owner_unverified')]*3
                if reason=='unknown_operations' else [ops,dict(ops,listener_inode=503),ops])
            def get(port,route,**kwargs):
                self.assertNotIn('credentials',kwargs)
                self.assertNotEqual(route,'/v1/capabilities')
                return f.reply(18107,route,version=5)
            with patch.object(identity,'release_identity',return_value=dict(
                    binary_sha256=('b' if reason=='wrong_elf' else 'a')*64,
                    protocol_sha256=f.configuration()['protocol_source_sha256'])), \
                 patch.object(identity,'process',return_value=owner), \
                 patch.object(identity,'operations',side_effect=sequence), \
                 patch.object(transport,'get',side_effect=get):
                report=diagnose.snapshot(18107,12345,path,operations_port=18108,client_policy_path=path)
            self.assertEqual(report['state'],'UNKNOWN')
            self.assertIn('error',report['probes']['/v1/capabilities'])


class Supervisor(unittest.TestCase):
    def probe(self, *, replaced=False, wrong_config=False):
        primary=instance();ops=dict(primary,listener_inode=502)
        config=f.configuration(version=5)
        if wrong_config:config['listeners']['operations']['port']=18109
        calls=[]
        def get(port,path,**kwargs):
            calls.append((port,path));self.assertEqual(port,18108)
            reply=f.reply(18107,path,version=5)
            if path=='/v1/config':reply['body']=json.dumps(config)
            return reply
        with patch.object(identity,'process',return_value=primary), \
             patch.object(identity,'operations',side_effect=[ops,dict(ops,listener_inode=503) if replaced else ops]), \
             patch.object(transport,'get',side_effect=get):
            result=sup.probe(12345,18107,18108)
        self.assertEqual(len(calls),4)
        return result

    def test_supervisor_checks_both_owners_but_never_probes_busy_public_port(self):
        result=self.probe()
        self.assertEqual(result['operations_port'],18108)
        self.assertEqual(result['observability_version'],5)

    def test_supervisor_rejects_replaced_listener_and_wrong_configuration(self):
        with self.assertRaises(RuntimeError):self.probe(replaced=True)
        with self.assertRaises(c.InvalidObservation):self.probe(wrong_config=True)


class Packages(Files):
    fixture=PackageTests.fixture

    def test_new_operations_package_pins_port_command_and_source_dependencies(self):
        binary,release,model=self.fixture()
        receipt=pkg.prepare(binary,release,model,self.root/'package',port=18107,operations_port=18108)
        manifest=pkg.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],6)
        command=pkg.command(receipt['package'],manifest)
        self.assertEqual(command[command.index('--operations-port')+1],'18108')
        self.assertNotIn('host_resources',manifest)
        self.assertIn('source/tools/serve_service/resources.py',manifest['files'])
        path=Path(receipt['package'])/'manifest.json'
        manifest['settings']['operations_port']=18107;pkg.save(path,manifest)
        with self.assertRaises(RuntimeError):pkg.verify(receipt['package'],pkg.sha(path))

    def test_port_is_explicit_bounded_and_legacy_packages_cannot_smuggle_it(self):
        for port in (True,-1,1,65536,18107):
            with self.assertRaises(RuntimeError):pkg.settings(18107,0,operations_port=port)
        binary,release,model=self.fixture()
        receipt=pkg.prepare(binary,release,model,self.root/'package',port=18107)
        manifest=pkg.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],3)
        self.assertNotIn('--operations-port',pkg.command(receipt['package'],manifest))
        manifest['settings']['operations_port']=18108
        path=Path(receipt['package'])/'manifest.json';pkg.save(path,manifest)
        with self.assertRaises(RuntimeError):pkg.verify(receipt['package'],pkg.sha(path))


class Gateway(Files):
    def policy(self):
        return dict(example(18443,18107),version=2,allowed_client_cidrs=['127.0.0.1/32'],
                    upstream_operations_port=18108)

    def test_operations_port_is_fixed_numeric_distinct_and_explicit_v2(self):
        p=self.policy()
        for port in (0,True,-1,65536,18107,18443,'18108;include bad'):
            with self.assertRaises(ValueError):g.policy(dict(p,upstream_operations_port=port))
        with self.assertRaises(ValueError):g.policy(dict(example(),upstream_operations_port=18108))
        text=g.render(p,self.root)
        self.assertIn('default 18107;',text);self.assertIn('control 18108;',text)
        self.assertIn('proxy_pass http://127.0.0.1:$q4t_upstream_port;',text)
        self.assertNotIn('$http_x_q4t_upstream',text)

    def test_real_nginx_syntax_and_package_pin_both_upstreams(self):
        cert,key=certificate(self.root)
        path=self.root/'front.json';g.save(path,json.dumps(self.policy()).encode())
        receipt=g.prepare(path,json.loads(VENDOR.read_text())['binary'],cert,key,self.root/'gateway',trusted_ca=cert)
        manifest=g.check(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['policy']['upstream_operations_port'],18108)
        self.assertFalse((Path(receipt['package'])/'runtime/nginx.pid').exists())

    def test_gateway_backend_requires_bilateral_port_and_stable_operations_identity(self):
        owner=dict(pid=12345,start_ticks=12000,boot_id='1'*36)
        primary=instance();ops=dict(primary,listener_inode=502)
        manifest=dict(settings=dict(port=18107,operations_port=18108),files={'q4t':'a'*64},protocol_sha256='c'*64)
        state=dict(controller_live=True,state='READY',worker=dict(backend=owner))
        probe=dict(process=primary,operations_process=ops,protocol='c'*64,client_policy=None)
        with patch.object(service,'status',return_value=state), \
             patch.object(pkg,'read',return_value=dict(package='fixture',manifest_sha256='d'*64)), \
             patch.object(pkg,'verify',return_value=manifest), \
             patch.object(sup,'process_identity',return_value=owner), \
             patch.object(identity,'process',return_value=primary), \
             patch.object(identity,'operations',return_value=ops), \
             patch.object(sup,'probe',return_value=probe) as observe:
            value=g.check_backend(self.root,dict(policy=self.policy()))
            self.assertEqual(value['operations_process'],ops)
            observe.assert_called_once_with(12345,18107,18108)
            wrong=self.policy();wrong.pop('upstream_operations_port')
            with self.assertRaises(ValueError):g.check_backend(self.root,dict(policy=wrong))
            probe['operations_process']=dict(ops,listener_inode=503)
            with self.assertRaises(ValueError):g.check_backend(self.root,dict(policy=self.policy()))


if __name__=='__main__':unittest.main()
