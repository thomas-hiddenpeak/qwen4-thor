"""Decode completed ChatKit attachment previews in Chromium and compare source pixels."""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


def check(case):
    wire = json.loads((case / 'create/sdk-result.json').read_bytes())
    fixtures = json.loads((case / 'plan.json').read_bytes())['fixtures']
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-gpu'])
        page = browser.new_page()
        for fixture, row in zip(fixtures, wire['rows'], strict=True):
            views = row['wire']
            receipt = views['upload']
            attachments = {
                'upload': receipt,
                'browser': next(item for item in views['browser']['data']
                                if item['type'] == 'user_message')['attachments'][0],
                'management': next(item for item in views['management']['data']
                                   if item['type'] == 'chatkit.user_message')['attachments'][0],
                'browser_after_delete': views['browser_after_delete'],
                'management_after_delete': views['management_after_delete'],
            }
            for attachment in attachments.values():
                assert attachment['id'] == receipt['id'] and attachment['type'] == 'image'
                assert attachment['preview_url'] == receipt['preview_url']
                assert 'upload_descriptor' not in attachment
            url = receipt['preview_url']
            assert url.startswith('data:image/png;base64,') and len(url) <= 65558
            with Image.open(fixture['path']) as source:
                # The HTTP fixtures are solid colors, including the actual JPEG decode.
                reference = source.convert('RGBA')
                assert len(reference.getcolors(2) or []) == 1
                expected_size = [round(value * min(1, 96 / max(source.size)))
                                 for value in source.size]
                expected = reference.resize(expected_size).tobytes()
            # Use the actual image element and canvas readback, independent of Pillow.
            for view, attachment in attachments.items():
                rendered = page.evaluate("""async url => {
                    const image = new Image();
                    image.src = url;
                    await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = image.naturalWidth;
                    canvas.height = image.naturalHeight;
                    const context = canvas.getContext('2d');
                    context.drawImage(image, 0, 0);
                    const rgba = context.getImageData(0, 0, canvas.width, canvas.height).data;
                    return {size: [canvas.width, canvas.height],
                            pixels: btoa(String.fromCharCode(...rgba))};
                }""", attachment['preview_url'])
                assert rendered['size'] == expected_size, view
                assert base64.b64decode(rendered['pixels'], validate=True) == expected, view
            results.append(dict(upload=row['mode'], source_mime=row['mime'],
                                size=expected_size, views=list(attachments),
                                pixel_sha256=hashlib.sha256(expected).hexdigest(),
                                preview_bytes=len(base64.b64decode(url.split(',', 1)[1], validate=True))))
        version = browser.version
        browser.close()
    return dict(browser='Chromium', version=version, rows=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', type=Path)
    print(json.dumps(check(parser.parse_args().case), indent=2))
