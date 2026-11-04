"""Real OpenSSL/MemoryBIO material checks, private packages and clock boundaries."""
import copy
import json
import math
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from tools.serve_gateway import gateway as g,managed as m,tls,tls_fixture as f
from tools.serve_gateway.test_gateway import example,VENDOR


def handshake(cert,key,ca,host=f.HOST):
    server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server.load_cert_chain(cert,key)
    client=ssl.create_default_context(cafile=ca)
    si,so,ci,co=[ssl.MemoryBIO() for _ in range(4)]
    ss=server.wrap_bio(si,so,server_side=True);cs=client.wrap_bio(ci,co,server_hostname=host)
    done=set()
    for _ in range(64):
        for label,sock in [('server',ss),('client',cs)]:
            if label in done:continue
            try:sock.do_handshake();done.add(label)
            except ssl.SSLWantReadError:pass
        if data:=so.read():ci.write(data)
        if data:=co.read():si.write(data)
        if len(done)==2:return tls.sha(cs.getpeercert(binary_form=True))
    raise AssertionError('MemoryBIO handshake bound')


class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent=g.ROOT/'build/tls-lifecycle-tests';parent.mkdir(exist_ok=True)
        cls.root=Path(tempfile.mkdtemp(dir=parent));cls.now=int(time.time())
        cls.ca=f.ca(cls.root/'ca');cls.other=f.ca(cls.root/'other-ca')
        cls.intermediate=f.issue(cls.root/'intermediate','intermediate',cls.ca,cls.now-60,cls.now+40*86400,is_ca=True)
        cls.issued=f.issue(cls.root/'leaf','leaf',cls.intermediate,cls.now-60,cls.now+10*86400)
        cls.cert=f.chain(cls.root/'leaf',cls.issued[0],cls.intermediate[0])
        cls.nginx=json.loads(VENDOR.read_text())['binary']

    def leaf(self,name,start,end,**kw):
        value=f.issue(self.root/name,name,self.intermediate,start,end,**kw)
        return f.chain(self.root/name,value[0],self.intermediate[0]),value[1]

    def inspect(self,cert=None,key=None,**kw):
        return tls.inspect(cert or self.cert,key or self.issued[1],f.HOST,**kw)

    def test_same_ca_two_distinct_leafs_actual_verified_handshakes(self):
        second,key=self.leaf('renewal',self.now-30,self.now+20*86400)
        for cert,private in ((self.cert,self.issued[1]),(second,key)):
            meta=self.inspect(cert,private,trusted_ca=self.ca[0])
            self.assertEqual(meta['trust'],'explicit_ca')
            self.assertEqual(handshake(cert,private,self.ca[0]),meta['chain'][0]['sha256'])
            self.assertEqual(meta['not_after'],min(v['not_after'] for v in meta['chain']+[meta['anchor']]))
        self.assertNotEqual(tls.certificates(self.cert.read_bytes())[0][1],tls.certificates(second.read_bytes())[0][1])

    def test_unconfigured_trust_is_explicit_and_not_a_handshake_claim(self):
        meta=self.inspect();self.assertEqual(meta['trust'],'not_configured');self.assertIsNone(meta['anchor'])
        self.assertFalse(meta['revocation_checked'])
        with self.assertRaises(ssl.SSLCertVerificationError):handshake(self.cert,self.issued[1],self.other[0])

    def test_wrong_ca_hostname_and_missing_chain_rejected(self):
        for cert,ca,host,reason in ((self.cert,self.other[0],f.HOST,'chain'),
            (self.cert,self.ca[0],'wrong.internal.test','hostname'),
            (self.issued[0],self.ca[0],f.HOST,'chain')):
            with self.subTest(reason=reason),self.assertRaisesRegex(tls.Invalid,reason):
                tls.inspect(cert,self.issued[1],host,trusted_ca=ca)
        with self.assertRaises(ssl.SSLCertVerificationError):handshake(self.cert,self.issued[1],self.ca[0],'wrong.internal.test')

    def test_future_expired_and_short_lived_leaf_refused(self):
        for label,start,end,reason in (('future',600,86400,'not_yet_valid'),
            ('expired',-86400,-10,'expired'),('short',-60,3500,'insufficient_lifetime')):
            cert,key=self.leaf(label,self.now+start,self.now+end)
            with self.subTest(label=label),self.assertRaisesRegex(tls.Invalid,reason):self.inspect(cert,key)

    def test_client_only_usage_refused(self):
        cert,key=self.leaf('client-only',self.now-60,self.now+86400,purpose='clientAuth')
        with self.assertRaisesRegex(tls.Invalid,'server_purpose'):self.inspect(cert,key)

    def test_future_or_expired_intermediate_cannot_hide_behind_valid_leaf(self):
        for name,start,end in (('future-ca',600,86400),('expired-ca',-86400,-10)):
            intermediate=f.issue(self.root/name,name,self.ca,self.now+start,self.now+end,is_ca=True)
            leaf=f.issue(self.root/(name+'-leaf'),'valid',intermediate,self.now-60,self.now+2*86400)
            cert=f.chain(self.root/(name+'-leaf'),leaf[0],intermediate[0])
            with self.assertRaisesRegex(tls.Invalid,'not_yet_valid|expired'):self.inspect(cert,leaf[1])

    def test_anchor_lifetime_is_included_before_trust_verification(self):
        anchor=f.issue(self.root/'expired-anchor','expired-anchor',self.ca,self.now-86400,self.now-10,is_ca=True)
        with self.assertRaisesRegex(tls.Invalid,'expired'):self.inspect(trusted_ca=anchor[0])
        with self.assertRaisesRegex(tls.Invalid,'anchor_invalid'):self.inspect(trusted_ca=self.issued[0])

    def test_key_pair_and_encrypted_key_rejected_without_prompt(self):
        with self.assertRaisesRegex(tls.Invalid,'key_pair'):self.inspect(key=self.other[1])
        encrypted=self.root/'encrypted.key'
        g.save(encrypted,tls.command(['pkey','-aes256','-passout','pass:fixture-only'],self.issued[1].read_bytes()))
        with self.assertRaisesRegex(tls.Invalid,'encrypted_key'):self.inspect(key=encrypted)

    def test_pem_duplicates_junk_and_count_bounds(self):
        raw=self.cert.read_bytes()
        for data in (b'',raw+b'junk',raw+raw,b'x'*(tls.MAX_BYTES+1),raw.replace(b'BEGIN CERTIFICATE',b'BEGIN PRIVATE KEY')):
            with self.subTest(size=len(data)),self.assertRaises(tls.Invalid):tls.certificates(data)
        with self.assertRaises(tls.Invalid):tls.certificates(raw,1)

    def test_validity_boundary_warning_and_unknown(self):
        meta=self.inspect();end=meta['not_after'];start=meta['not_before']
        for now,wanted in ((start-1,'NOT_YET_VALID'),(start,'VALID'),(end-tls.WARN_SECONDS,'EXPIRING'),
            (end-.001,'EXPIRING'),(end,'EXPIRED'),(math.nan,'UNKNOWN'),(-1,'UNKNOWN')):
            with self.subTest(wanted=wanted):self.assertEqual(tls.observe(meta,now)['state'],wanted)
        for value in ({},None,dict(meta,not_after=True)):
            self.assertEqual(tls.observe(value,self.now)['state'],'UNKNOWN')
        with self.assertRaisesRegex(tls.Invalid,'insufficient_lifetime'):tls.admit(meta,end-3600)
        self.assertEqual(tls.admit(meta,end-3601)['state'],'EXPIRING')

    def test_copied_material_is_validated_when_original_changes(self):
        root=self.root/'copied';root.mkdir(mode=0o700)
        policy=root/'policy.json';g.save(policy,json.dumps(example()).encode())
        cert=root/'input.pem';g.save(cert,self.cert.read_bytes())
        original=g.certificate_check
        def check(certificate,key,hostname,**kw):
            self.assertEqual(Path(certificate).parent,root/'package')
            cert.write_bytes(b'changed after copy')
            return original(certificate,key,hostname,**kw)
        with patch.object(g,'certificate_check',check):
            receipt=g.prepare(policy,self.nginx,cert,self.issued[1],root/'package',trusted_ca=self.ca[0])
        manifest=g.check(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual((manifest['version'],manifest['tls']['trust']),(3,'explicit_ca'))
        self.assertNotIn('PRIVATE KEY',json.dumps(manifest))
        target=root/'package/trusted-ca.pem';target.write_bytes(self.other[0].read_bytes())
        with self.assertRaisesRegex(ValueError,'identity'):g.check(receipt['package'],receipt['manifest_sha256'])

    def test_pinned_managed_entry_includes_validator_and_checks_metadata(self):
        root=self.root/'managed';root.mkdir(mode=0o700)
        policy=root/'policy.json';g.save(policy,json.dumps(example()).encode())
        receipt=m.prepare(policy,self.nginx,self.cert,self.issued[1],root/'package',trusted_ca=self.ca[0])
        manifest,inner=m.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],4);self.assertIn('tools/serve_gateway/tls.py',manifest['sources'])
        result=subprocess.run([sys.executable,'-I','-B',str(root/'package/source/tools/serve_gateway/gateway.py'),
            'check','--package',inner['nginx']['path'].rsplit('/',1)[0]+'/gateway',
            '--manifest-sha256',manifest['gateway']['manifest_sha256']],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        value=copy.deepcopy(inner);value['tls']['not_after']+=1
        path=root/'package/gateway/manifest.json';path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'metadata'):g.check(path.parent,g.sha(path))


if __name__=='__main__':unittest.main()
