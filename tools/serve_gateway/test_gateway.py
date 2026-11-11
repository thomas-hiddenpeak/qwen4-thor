"""CPU configuration, filesystem, certificate and real nginx syntax checks."""
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.serve_gateway import gateway as g

VENDOR = g.ROOT/'.q4t-work/serve-gateway-20261103/vendor/receipt.json'


def certificate(directory, name='q4t.internal.test'):
    cert, key = directory/'certificate.pem', directory/'key.pem'
    result = subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048',
        '-nodes','-days','2','-subj','/CN='+name,'-addext','subjectAltName=DNS:'+name,
        '-keyout',str(key),'-out',str(cert)], capture_output=True, timeout=15)
    g.require(result.returncode == 0, 'fixture certificate generation failed')
    key.chmod(0o600)
    return cert, key


def example(port=18443, upstream=18124):
    return dict(version=1,listen_host='127.0.0.1',listen_port=port,
        server_name='q4t.internal.test',upstream_port=upstream,
        clients=[dict(id='alpha',api_key=secrets.token_urlsafe(32)),
                 dict(id='beta',api_key=secrets.token_urlsafe(32))])


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent=g.ROOT/'build/serve-gateway-tests'
        parent.mkdir(exist_ok=True)
        # Preserve private original nginx diagnostics even on failure.
        cls.root=Path(tempfile.mkdtemp(dir=parent))
        cls.cert,cls.key=certificate(cls.root)
        cls.nginx=json.loads(VENDOR.read_text())['binary']

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        self.folder=self.root/secrets.token_hex(6)
        self.folder.mkdir(mode=0o700)
        self.p=example()

    def prepare(self):
        policy=self.folder/'policy.json'
        policy.write_text(json.dumps(self.p));policy.chmod(0o600)
        return g.prepare(policy,self.nginx,self.cert,self.key,self.folder/'package')

    def test_policy_rejects_remote_wildcard_and_unbounded_limits(self):
        cases=[('listen_host','0.0.0.0'),('listen_host','8.8.8.8'),
            ('listen_host','localhost'),('listen_host','::1'),('listen_host',167772161),
            ('listen_port',True),('listen_port',443),('global_inflight',9),
            ('per_client_inflight',0),('body_bytes',96*1024**2+1),
            ('upstream_idle_seconds',0),('burst',0),('version',True),
            ('server_name','good;include bad'),('server_name','a..b'),
            ('server_name','-a.local'),('clients',[]),('extra',1)]
        for name,value in cases:
            with self.subTest(field=name,value=value):
                p=copy.deepcopy(self.p);p[name]=value
                with self.assertRaises(ValueError):g.policy(p)
        for host in ('10.1.2.3','172.16.1.2','192.168.2.3'):
            self.assertEqual(g.policy(dict(self.p,listen_host=host))['listen_host'],host)

    def test_keys_and_client_ids_are_strict_and_unique(self):
        for key in ('x'*31,'x'*129,'a'*32+'$','a'*32+'"','a'*32+'\n'):
            p=copy.deepcopy(self.p);p['clients'][0]['api_key']=key
            with self.assertRaises(ValueError):g.policy(p)
        for field in ('id','api_key'):
            p=copy.deepcopy(self.p);p['clients'][1][field]=p['clients'][0][field]
            with self.assertRaises(ValueError):g.policy(p)
        p=copy.deepcopy(self.p);p['clients'][0]['extra']=1
        with self.assertRaises(ValueError):g.policy(p)
        with self.assertRaises(ValueError):g.parse('{"version":1,"version":1}')

    def test_real_nginx_parse_and_private_package_verification(self):
        receipt=self.prepare()
        manifest=g.check(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['policy']['client_ids'],['alpha','beta'])
        raw=(Path(receipt['package'])/'manifest.json').read_text()
        for client in self.p['clients']:self.assertNotIn(client['api_key'],raw)
        self.assertFalse(manifest['automatic_start'])
        self.assertFalse(manifest['production_deployment_accepted'])
        self.assertFalse((Path(receipt['package'])/'runtime/nginx.pid').exists())

    def test_changed_input_and_nonprivate_directory_refuse_start(self):
        receipt=self.prepare();directory=Path(receipt['package'])
        directory.chmod(0o750)
        with self.assertRaisesRegex(ValueError,'permissions'):g.check(directory,receipt['manifest_sha256'])
        directory.chmod(0o700)
        with (directory/'nginx.conf').open('a') as stream:stream.write('\n')
        with self.assertRaisesRegex(ValueError,'identity'):g.check(directory,receipt['manifest_sha256'])

    def test_secret_permissions_symlinks_and_named_pipes_rejected(self):
        file=self.folder/'private';file.write_text('secret');file.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'0600'):g.read_file(file,private=True)
        file.chmod(0o600)
        link=self.folder/'link';link.symlink_to(file)
        with self.assertRaises(OSError):g.read_file(link,private=True)
        fifo=self.folder/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaisesRegex(ValueError,'regular'):g.read_file(fifo,private=True)
        with self.assertRaisesRegex(ValueError,'size'):g.read_file(file,maximum=2)

    def test_certificate_hostname_key_pair_and_expiry_are_real_checks(self):
        g.certificate_check(self.cert,self.key,'q4t.internal.test')
        with self.assertRaisesRegex(ValueError,'hostname'):
            g.certificate_check(self.cert,self.key,'wrong.internal.test')
        other=self.folder/'other';other.mkdir()
        _,key=certificate(other)
        with self.assertRaises(Exception):g.certificate_check(self.cert,key,'q4t.internal.test')
        # Real openssl checkend against a bound longer than the real fixture TTL.
        r=subprocess.run(['/usr/bin/openssl','x509','-in',str(self.cert),'-noout',
                          '-checkend','259200'],capture_output=True,timeout=10)
        self.assertNotEqual(r.returncode,0)

    def test_no_overwrite_or_output_escape(self):
        receipt=self.prepare()
        with self.assertRaises(ValueError):
            g.prepare(self.folder/'policy.json',self.nginx,self.cert,self.key,receipt['package'])
        with self.assertRaisesRegex(ValueError,'outside'):
            g.prepare(self.folder/'policy.json',self.nginx,self.cert,self.key,'/tmp/q4t-gateway')
        with self.assertRaises(ValueError):g.quoted('/tmp/x;include')
        with self.assertRaises(ValueError):g.quoted('/tmp/x$var')

    def test_control_and_data_have_separate_complete_bounds(self):
        text=g.render(self.p,self.folder)
        self.assertIn('limit_conn all_data 8;',text)
        self.assertIn('limit_conn all_control 8;',text)
        self.assertIn('proxy_next_upstream off;',text)
        self.assertIn('proxy_pass_request_headers off;',text)
        self.assertNotIn('proxy_set_header Authorization',text)
        self.assertNotIn('$request_body',text)
        self.assertNotIn('location /metrics',text)
        self.assertIn('"~^POST:/v1/completions$" data;', text)
        self.assertIn('"~^POST:/v1/moderations$" data;', text)
        self.assertIn('"~^GET:/v1/models/[^?#]+$" control;', text)
        self.assertIn('"~^DELETE:/v1/models/[^?#]+$" control;', text)
        with self.assertRaises(ValueError):g.policy(dict(self.p,global_inflight=1))

    def test_backend_identity_requires_ready_pinned_loopback(self):
        from tools.serve_service import service,package,supervisor
        from tools.serve_ops import identity
        owner=dict(pid=999,start_ticks=123,boot_id='fixture')
        status=dict(controller_live=True,state='READY',worker=dict(backend=owner))
        control=dict(package='fixture',manifest_sha256='a'*64)
        manifest=dict(settings=dict(port=18124),files={'q4t':'b'*64})
        with mock.patch.object(service,'status',return_value=status), \
             mock.patch.object(package,'read',return_value=control), \
             mock.patch.object(package,'verify',return_value=manifest), \
             mock.patch.object(supervisor,'process_identity',return_value=owner), \
             mock.patch.object(identity,'process') as observe:
            observe.return_value=dict(listener_scope='wildcard',executable_sha256='b'*64,
                                      listener_inode=123)
            with self.assertRaisesRegex(ValueError,'loopback'):
                g.check_backend(self.folder,dict(policy=dict(upstream_port=18124)))
            observe.return_value['listener_scope']='loopback'
            self.assertEqual(g.check_backend(self.folder,dict(policy=dict(upstream_port=18124)))['backend'],owner)
            status['state']='STOPPED'
            with self.assertRaisesRegex(ValueError,'READY'):
                g.check_backend(self.folder,dict(policy=dict(upstream_port=18124)))


if __name__=='__main__':unittest.main()
