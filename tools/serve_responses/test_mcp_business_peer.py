"""Frozen private fixture PKI must survive real authenticated TLS reads."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest

from tools.serve_responses import mcp_business_peer as peer

ROOT=Path(__file__).resolve().parents[2]


class FixturePki(unittest.TestCase):
    def test_frozen_and_legacy_pki_both_authenticate_real_document_reads(self):
        for frozen in (True,False):
            with self.subTest(frozen=frozen),tempfile.TemporaryDirectory(
                dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
                root=Path(directory)
                material=peer.prepare_tls(root) if frozen else None
                hashes={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        for p in material.values()} if frozen else {}
                server=peer.ProjectMcp(ROOT,root,0,tls=material)
                try:
                    def rpc(method,session=None,authorized=True,params=None):
                        context=ssl.create_default_context(cafile=str(server.ca[0]))
                        connection=http.client.HTTPSConnection('localhost',server.server.server_port,
                                                               context=context,timeout=3)
                        headers={'Authorization':'Bearer '+(server.secret if authorized else 'invalid'),
                                 'Content-Type':'application/json'}
                        if session:headers['MCP-Session-Id']=session
                        body=dict(jsonrpc='2.0',id=1,method=method,params=params or {})
                        try:
                            connection.request('POST','/mcp',json.dumps(body),headers)
                            result=connection.getresponse();raw=result.read()
                            return result.status,result.getheader('MCP-Session-Id'),json.loads(raw) if raw else None
                        finally:connection.close()
                    self.assertEqual(rpc('initialize',authorized=False)[0],403)
                    status,session,_=rpc('initialize');self.assertEqual(status,200)
                    self.assertEqual(rpc('tools/list',session)[2]['result'],peer.catalog())
                    result=rpc('tools/call',session,params=dict(name='read_project_record',arguments=peer.ARGUMENTS))
                    self.assertEqual(result[0],200)
                    self.assertEqual(result[2]['result']['structuredContent'],peer.DocumentSource(ROOT).read()[0])
                    self.assertEqual(len(server.snapshot()['calls']),1)
                    self.assertEqual(hashes,{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in hashes})
                finally:server.close()

    def test_incomplete_frozen_pki_is_rejected_before_listener(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            with self.assertRaisesRegex(RuntimeError,'exact private fixture PKI'):
                peer.ProjectMcp(ROOT,Path(directory),0,tls=dict(certificate='missing'))

    def test_frozen_peer_needs_no_certificate_issuer_in_sealed_gateway_namespace(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            root=Path(directory);material=peer.prepare_tls(root)
            script='''
import json,sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import tools
gateway=types.ModuleType('tools.serve_gateway')
gateway.__path__=[]
sys.modules['tools.serve_gateway']=gateway
tools.serve_gateway=gateway
from tools.serve_responses.mcp_business_peer import ProjectMcp
assert 'tools.serve_gateway.tls_fixture' not in sys.modules
args=json.load(sys.stdin)
server=ProjectMcp(Path(sys.argv[1]),Path(args['output']),0,tls=args['tls'])
try:
 assert server.server.server_port>0
 assert 'tools.serve_gateway.tls_fixture' not in sys.modules
finally:server.close()
print('FROZEN_PKI_WITHOUT_ISSUER_PASS')
'''
            result=subprocess.run([sys.executable,'-I','-B','-c',script,str(ROOT)],
                input=json.dumps(dict(output=str(root),tls=material)),
                capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'FROZEN_PKI_WITHOUT_ISSUER_PASS')


if __name__=='__main__':unittest.main()
