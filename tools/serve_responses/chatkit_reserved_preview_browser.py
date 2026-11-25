"""Exercise the reservation URL in a browser that retains the first upload reply."""
import base64
import hashlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator
from PIL import Image
from playwright.sync_api import sync_playwright


def run(plan):
    base = plan['base']
    cert = plan['certificate']
    context = ssl.create_default_context(cafile=cert)
    origin = base.removesuffix('/v1')
    public = subprocess.check_output(['openssl', 'x509', '-in', cert, '-pubkey', '-noout'])
    der = subprocess.check_output(['openssl', 'pkey', '-pubin', '-outform', 'DER'], input=public)
    pin = base64.b64encode(hashlib.sha256(der).digest()).decode()

    def api(path, body, key, beta=None):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}
        if beta:
            headers['OpenAI-Beta'] = beta
        req = Request(base + path, data=json.dumps(body).encode(), headers=headers)
        with urlopen(req, context=context, timeout=12) as res:
            return json.load(res)

    agent = api('/agents', {'model': plan['model']}, plan['key'], 'agents=v1')
    image_path = Path(plan['image'])
    original = image_path.read_bytes()
    expected = Image.open(io.BytesIO(original)).convert('RGBA')
    assert expected.size == (64, 32)
    expected_sha = hashlib.sha256(expected.tobytes()).hexdigest()
    spec = json.loads((Path(__file__).resolve().parents[2] /
                       'docs/serve-openapi-bundle-v1.json').read_bytes())
    shape = spec['paths']['/v1/chatkit']['post']['x-q4t-result-by-request-type']['attachments.create']
    validator = Draft202012Validator(dict(shape, components=spec['components']),
                                     format_checker=Draft202012Validator.FORMAT_CHECKER)
    reports = []
    with sync_playwright() as playwright:
        chrome = playwright.chromium.launch(args=['--disable-gpu', '--ignore-certificate-errors-spki-list=' + pin])
        page = chrome.new_page()
        html = origin + '/q4t-preview-fixture'
        page.route(html, lambda route: route.fulfill(content_type='text/html', body='<title>ChatKit image</title>'))
        page.goto(html)
        for end in ('delete', 'cancel', 'expire'):
            session = api('/chatkit/sessions', dict(user='preview-reader', workflow={'id': agent['id']},
                expires_after=dict(anchor='created_at', seconds=8 if end=='expire' else 120),
                chatkit_configuration=dict(file_upload=dict(enabled=True, max_files=2, max_file_size=1))),
                plan['key'], 'chatkit_beta=v1')
            def rpc(kind, params):
                return api('/chatkit', dict(type=kind, params=params), session['client_secret'])
            resource = rpc('attachments.create', dict(name='scene.png', size=len(original), mime_type='image/png'))
            validator.validate(resource)
            url = resource['preview_url']
            put = resource['upload_descriptor']
            assert resource['type']=='image' and url.startswith(put['url']+'/preview?ticket=ckpv_')
            assert put['headers']['Authorization'].split()[-1] not in url

            def get():
                return page.evaluate("""async url => {
                    const res = await fetch(url);
                    return [res.status,res.headers.get('Cache-Control'),res.headers.get('Content-Type')];
                }""", url)
            before = get()
            assert before[0] == 404 and 'no-store' in (before[1] or ''), before
            # Discard the actual PUT reply like clients that keep only the reservation.
            status = page.evaluate("""async a => (await fetch(a.descriptor.url, {
                method: a.descriptor.method, headers: a.descriptor.headers,
                body: Uint8Array.from(atob(a.bytes), c => c.charCodeAt(0))
            })).status""", dict(descriptor=put, bytes=base64.b64encode(original).decode()))
            assert status == 200, status
            after = get()
            assert after[0]==200 and 'no-store' in (after[1] or '') and after[2]=='image/png', after
            rendered = page.evaluate("""async url => {
                const img = new Image(); img.src = url; await img.decode();
                const c = document.createElement('canvas'); c.width=img.naturalWidth; c.height=img.naturalHeight;
                const ctx=c.getContext('2d'); ctx.drawImage(img,0,0);
                const data=ctx.getImageData(0,0,c.width,c.height).data;
                return [c.width,c.height,btoa(String.fromCharCode(...data))];
            }""", url)
            assert rendered[:2] == list(expected.size)
            actual_sha = hashlib.sha256(base64.b64decode(rendered[2],validate=True)).hexdigest()
            assert actual_sha == expected_sha
            if end == 'delete':
                rpc('attachments.delete', {'attachment_id':resource['id']})
            elif end == 'cancel':
                api('/chatkit/sessions/'+session['id']+'/cancel', {}, plan['key'], 'chatkit_beta=v1')
            else:
                time.sleep(max(0, session['expires_at'] - time.time() + .15))
            terminal = get()
            assert terminal[0] == (404 if end=='delete' else 401), (end, terminal)
            reports.append(dict(end=end, before=before[0], completed=after[0],
                                terminal=terminal[0], source_size=rendered[:2],
                                decoded_rgba_sha256=actual_sha))
        version = chrome.version
        chrome.close()
    return dict(rows=reports, chromium=version, preview_contract_checked=True)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))
