"""Synthetic browser integration. Every HTTP request is fulfilled locally."""

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile

from PIL import Image

from taobao_buyer import BuyerClient
from taobao_buyer.dom import SNAPSHOT
from taobao_buyer.extract import extract_product

PRODUCT = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
<h1 class="mainTitle--fixture">Synthetic tee</h1>
<div class="highlightPrice--fixture">¥99</div><div class="price--fixture">券后 ¥89 起</div>
<div class="BasicContent">衣身: 100% cotton<br>领口: Sorona blend<br>收货地址: Cotton Street</div>
<div class="skuItem--fixture"><span class="label">尺码</span><ul><li>0</li><li>1</li></ul></div>
<div class="PicGallery"><img src="https://img.alicdn.com/synthetic-gallery.jpg?tracking=private"></div>
<div class="detailContent"><p>Body: 100% cotton</p><p>Collar: Sorona blend</p>
<img alt="尺码表" src="https://img.alicdn.com/synthetic-chart.png?tracking=private"></div>
</body></html>'''
SEARCH = '''<!doctype html><html><head><meta charset="utf-8"></head><body><input type="file"
onchange="window.open('https://s.taobao.com/search?imgSearch=synthetic')"></body></html>'''
RESULTS = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
<a href="https://item.taobao.com/item.htm?id=123&amp;tracking=private"><span class="title">Synthetic tee</span>
<span class="price">¥99</span><img src="https://img.alicdn.com/synthetic-gallery.jpg"></a>
</body></html>'''


async def fixture(route):
    url = route.request.url
    if 'item.taobao.com' in url:
        html = PRODUCT
    elif 'imgSearch=synthetic' in url:
        html = RESULTS
    elif 's.taobao.com' in url:
        html = SEARCH
    else:
        await route.fulfill(status=200, content_type='image/png', body=b'')
        return
    await route.fulfill(status=200, content_type='text/html', body=html)


async def check():
    with tempfile.TemporaryDirectory(prefix='buyer-offline-') as temporary:
        profile = Path(temporary) / 'profile'
        async with BuyerClient(profile) as client:
            await client._context.route('**/*', fixture)
            await client._context.add_cookies([{'name':'cookie17','value':'synthetic-only',
                                               'domain':'.taobao.com','path':'/','secure':True}])
            page = await client._context.new_page()
            await page.goto('https://item.taobao.com/item.htm?id=123')
            await page.evaluate("localStorage.setItem('synthetic', 'restored')")
            snapshot = await page.evaluate(SNAPSHOT)
            result = extract_product(snapshot, page.url)
            assert result['price']['displayed'] == '¥99'
            assert result['price']['qualifications'] == ['券后 ¥89 起']
            assert result['variants'][0]['options'] == ['0', '1']
            assert any('Sorona' in claim for claim in result['material_claims'])
            assert result['size_chart_images'] == ['https://img.alicdn.com/synthetic-chart.png']
            assert 'private' not in json.dumps(result)
            assert 'Cotton Street' not in json.dumps(result)
        assert (profile / 'session.json').stat().st_mode & 0o777 == 0o600
        async with BuyerClient(profile) as restored:
            await restored._context.route('**/*', fixture)
            assert (await restored.session_status())['state'] == 'authenticated'
            page = await restored._context.new_page()
            await page.goto('https://item.taobao.com/item.htm?id=123')
            assert await page.evaluate("localStorage.getItem('synthetic')") == 'restored'
            data = io.BytesIO()
            Image.new('RGB', (16, 16), 'white').save(data, format='PNG')
            result = await restored.search_image(data.getvalue())
            assert [p['url'] for p in result['products']] == ['https://item.taobao.com/item.htm?id=123']
            await restored.session_clear()
            assert (await restored.session_status())['state'] == 'login_required'
            assert restored.policy.summary()['operations_last_day'] == 1
    return {'offline_browser': 'passed', 'session_restart': 'passed', 'image_popup': 'passed',
            'pid': os.getpid()}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(check())))
