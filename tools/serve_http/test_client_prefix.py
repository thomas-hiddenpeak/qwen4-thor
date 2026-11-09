"""Real standard-library HTTP prefix routing and closed base URL validation."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import unittest

from tools.examples.chat_client import ChatClient,ProtocolError,make_request


class PrefixTests(unittest.TestCase):
    def test_prefixed_client_keeps_path_and_authorization_on_actual_http(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                seen.append((self.path,self.headers.get('Authorization')))
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body=json.dumps(dict(id=request['request_id'],model=request['model'],created=0,
                    object='chat.completion',usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2),
                    choices=[dict(index=0,message=dict(role='assistant',content='42'),
                                  finish_reason='stop',logprobs=None)])).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            api=ChatClient(base+'/llm/thor/v1/',api_key='prefix-key')
            value=api.complete(make_request([dict(role='user',content='test')],[],stream=False))
            self.assertEqual(value.message['content'],'42')
            self.assertEqual(seen,[('/llm/thor/v1/chat/completions','Bearer prefix-key')])
            self.assertEqual(ChatClient(base).base,base+'/v1')
            self.assertEqual(ChatClient(base+'/v1').base,base+'/v1')
        finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def test_prefix_rejects_ambiguous_paths_credentials_and_queries(self):
        for suffix in ['/x','/x//v1','/x/../v1','/x/./v1','/%2e/v1','/a%2Fb/v1',
                       '/v1//','/x/v1?q=1','/x/v1#x','/'+'x'*513+'/v1']:
            with self.subTest(suffix=suffix),self.assertRaises(ProtocolError):
                ChatClient('http://example.test'+suffix)
        for url in ['http://user:secret@example.test/x/v1','ftp://example.test/x/v1']:
            with self.assertRaises(ProtocolError):ChatClient(url)


if __name__=='__main__':unittest.main()
