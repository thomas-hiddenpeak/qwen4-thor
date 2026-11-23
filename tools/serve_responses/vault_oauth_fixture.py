"""Loopback HTTPS token issuer for the Vault-to-MCP SDK business check."""
import base64
import http.server
import json
import secrets
import ssl
import threading
import urllib.parse


class TokenIssuer:
    def __init__(self, peer, *, retry_after=None):
        self.resource = peer.url
        self.client = 'q4t:fixture reader'
        self.client_secret = secrets.token_urlsafe(24) + '+'
        self.original = secrets.token_urlsafe(24)
        self.replacement = secrets.token_urlsafe(24)
        self.receipts = []
        issuer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = self.headers.get('Content-Length', '')
                if (self.path != '/token' or not length.isdecimal() or
                        int(length) > 16384 or self.headers.get('Content-Type') !=
                        'application/x-www-form-urlencoded'):
                    self.send_error(400)
                    return
                credentials = ':'.join(urllib.parse.quote_plus(x, safe='') for x in
                                       (issuer.client, issuer.client_secret))
                basic = 'Basic ' + base64.b64encode(credentials.encode()).decode()
                form = urllib.parse.parse_qs(self.rfile.read(int(length)).decode(),
                    strict_parsing=True, max_num_fields=5, keep_blank_values=True)
                token = (issuer.replacement if 'original' in issuer.receipts
                         else issuer.original)
                if self.headers.get_all('Authorization') != [basic] or form != {
                        'refresh_token':[token], 'grant_type':['refresh_token'],
                        'scope':['records.read'], 'resource':[issuer.resource]}:
                    self.send_error(403)
                    return
                if retry_after is not None and not issuer.receipts:
                    issuer.receipts.append('temporary')
                    wire = b'{"error":"temporarily_unavailable"}'
                    self.send_response(503)
                    self.send_header('Retry-After', str(retry_after))
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(wire)))
                    self.end_headers()
                    self.wfile.write(wire)
                    return
                exchanged = 'original' in issuer.receipts
                issuer.receipts.append('rotated' if exchanged else 'original')
                if exchanged:
                    # Model an issuer that consumed a rotating token before
                    # its connection vanished. No second receipt is available.
                    self.close_connection = True
                    return
                wire = json.dumps(dict(access_token=peer.secret, token_type='Bearer',
                    expires_in=3600, refresh_token=issuer.replacement)).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(peer.certificate, peer.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs=dict(poll_interval=.02))
        self.thread.start()

    def grant(self):
        return dict(token_endpoint=f'https://localhost:{self.server.server_port}/token',
            client_id=self.client, refresh_token=self.original, scope='records.read',
            resource=self.resource, token_endpoint_auth=dict(
                type='client_secret_basic', client_secret=self.client_secret))

    def close(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
