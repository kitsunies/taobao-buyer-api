import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from taobao_buyer.browser import BuyerClient, access_error
from taobao_buyer.errors import SourcingError
from taobao_buyer.policy import Policy


def run(coro):
    return asyncio.run(coro)


def client(tmp_path):
    c = BuyerClient(tmp_path)
    c.profile.acquire()
    c.policy = Policy(tmp_path)
    c.ready = True
    return c


@pytest.mark.parametrize('status,url,text,code', [
    (403, 'https://item.taobao.com/item.htm?id=1', '', 'verification_required'),
    (429, 'https://item.taobao.com/item.htm?id=1', '', 'rate_limited'),
    (200, 'https://item.taobao.com/item.htm?id=1', '请完成验证', 'verification_required'),
    (200, 'https://item.taobao.com/_____tmd_____/punish', '', 'verification_required'),
    (200, 'https://login.taobao.com/member/login.jhtml', '', 'login_required'),
    (503, 'https://item.taobao.com/item.htm?id=1', '', 'upstream_access'),
])
def test_classify_access(status, url, text, code):
    assert access_error(status, url, text).code == code
    assert 'item.taobao' not in json.dumps(access_error(status, url, text).as_dict())


def test_promotion_is_not_login_gate():
    assert access_error(200, 'https://item.taobao.com/item.htm?id=1', '登录后享优惠') is None
    assert access_error(200, 'https://login.taobao.com', login=True) is None


def response(status=200, text='', headers=None, page=None, resource='xhr'):
    return SimpleNamespace(status=status, url='https://h5api.m.taobao.com/h5/mtop.test/1.0/',
        headers=headers or {'content-type': 'application/json'}, text=AsyncMock(return_value=text),
        request=SimpleNamespace(frame=SimpleNamespace(page=page), resource_type=resource))


def test_chunked_200_challenge_persists_cooldown(tmp_path):
    c = client(tmp_path)
    run(c._observe_response(response(text='{"ret":["FAIL_SYS_USER_VALIDATE"]}')))
    assert c._access_error.code == 'verification_required'
    assert Policy(tmp_path).summary()['retry_after'] >= 1799
    c.profile.release()


def test_numeric_429_and_no_retry(tmp_path):
    c = client(tmp_path)
    r = response(429, headers={'retry-after': '90'})
    run(c._observe_response(r))
    assert 89 <= c._access_error.retry_after <= 90
    r.text.assert_not_awaited()
    route = SimpleNamespace(request=SimpleNamespace(url='https://item.taobao.com/item.htm?id=1'),
                            continue_=AsyncMock(), abort=AsyncMock())
    run(c._route(route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    c.profile.release()


def test_active_login_poll_is_not_redirect(tmp_path):
    c = client(tmp_path)
    c._login_page = object()
    r = response(page=c._login_page, resource='document')
    r.url = 'https://login.taobao.com/member/login.jhtml'
    run(c._observe_response(r))
    assert c._access_error is None
    c.profile.release()


def test_expired_cookies_and_no_private_session_response(tmp_path):
    c = client(tmp_path)
    c._context = SimpleNamespace(cookies=AsyncMock(return_value=[
        {'name':'cookie17', 'value':'PRIVATE_TOKEN', 'expires':time.time()-1}]))
    result = run(c.session_status())
    assert result['state'] == 'login_required'
    assert 'PRIVATE' not in json.dumps(result)
    assert c.policy.summary()['operations_last_day'] == 0
    c.profile.release()


def test_session_state_private_and_clear_preserves_policy(tmp_path):
    async def scenario():
        c = client(tmp_path)
        c.policy.cooldown('600')
        c._context = SimpleNamespace(storage_state=AsyncMock(return_value={'cookies':[], 'origins':[]}),
                                     close=AsyncMock())
        await c._save_session()
        assert (tmp_path/'session.json').stat().st_mode & 0o777 == 0o600
        c._new_context = AsyncMock()
        assert (await c.session_clear())['state'] == 'cleared'
        assert not (tmp_path/'session.json').exists()
        assert Policy(tmp_path).summary()['retry_after'] >= 599
        c.profile.release()
    run(scenario())


def test_refresh_keeps_browser_context(tmp_path):
    async def scenario():
        c = client(tmp_path)
        context = c._context = object()
        refresh = SimpleNamespace(count=AsyncMock(return_value=1),
                                 is_visible=AsyncMock(return_value=True), click=AsyncMock())
        refresh.first = refresh
        c._login_page = SimpleNamespace(frames=[SimpleNamespace(get_by_text=lambda *a, **k: refresh)])
        page = c._login_page
        c._check_page = AsyncMock()
        c._login_result = AsyncMock(return_value={'state':'waiting','qr_png_base64':'synthetic'})
        result = await c.login_refresh()
        assert result['state'] == 'waiting' and c._context is context and c._login_page is page
        refresh.click.assert_awaited_once()
        c.profile.release()
    run(scenario())


def test_qr_expiry(tmp_path):
    c = client(tmp_path)
    c._login_page = SimpleNamespace(frames=[SimpleNamespace(locator=lambda _: SimpleNamespace(
        inner_text=AsyncMock(return_value='二维码已失效')))])
    c._check_page = AsyncMock()
    c._authenticated = AsyncMock(return_value=False)
    assert run(c._login_result()) == {'state':'expired'}
    c.profile.release()


def test_login_qr_survives_frame_body_timeout(tmp_path):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    c = client(tmp_path)
    element = SimpleNamespace(wait_for=AsyncMock(), screenshot=AsyncMock(return_value=b'png'))
    canvas = SimpleNamespace(count=AsyncMock(return_value=1), first=element)

    def locator(selector):
        if selector == 'body':
            return SimpleNamespace(inner_text=AsyncMock(side_effect=PlaywrightTimeoutError('detached')))
        return canvas

    c._login_page = SimpleNamespace(frames=[SimpleNamespace(locator=locator)])
    c._check_page = AsyncMock()
    c._authenticated = AsyncMock(return_value=False)
    assert run(c._login_result()) == {'state':'waiting','qr_png_base64':'cG5n'}
    c.profile.release()


def test_hidden_qr_reports_layout_not_operation_failure(tmp_path):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    c = client(tmp_path)
    element = SimpleNamespace(wait_for=AsyncMock(side_effect=PlaywrightTimeoutError('hidden')))
    canvas = SimpleNamespace(count=AsyncMock(return_value=1), first=element)
    image = SimpleNamespace(count=AsyncMock(return_value=0), first=element)

    def locator(selector):
        if selector == 'body':
            return SimpleNamespace(inner_text=AsyncMock(return_value='登录'))
        return canvas if 'canvas' in selector else image

    c._login_page = SimpleNamespace(frames=[SimpleNamespace(locator=locator)])
    c._check_page = AsyncMock()
    c._authenticated = AsyncMock(return_value=False)
    with pytest.raises(SourcingError) as caught:
        run(c._login_result())
    assert caught.value.code == 'login_layout'
    c.profile.release()


def test_busy_prevents_second_operation(tmp_path):
    async def scenario():
        c = client(tmp_path)
        await c._lock.acquire()
        with pytest.raises(SourcingError, match='Another browser'):
            await c.session_status()
        c._lock.release()
        c.profile.release()
    run(scenario())


def test_response_task_errors_are_consumed(tmp_path):
    async def scenario():
        c = client(tmp_path)
        async def bad():
            raise OSError('PRIVATE_PATH')
        task = asyncio.create_task(bad())
        await asyncio.sleep(0)
        c._response_done(task)
        assert c._access_error.code == 'browser_observation'
        assert 'PRIVATE' not in str(c._access_error)
        c.profile.release()
    run(scenario())


def test_tmall_login_redirect_is_detected():
    assert access_error(200, 'https://login.tmall.com/').code == 'login_required'


def test_material_claims_do_not_reintroduce_private_attributes():
    from taobao_buyer.extract import extract_product
    result = extract_product({'title':'Synthetic tee',
        'attributes':[{'name':'收货地址','value':'Cotton Street'}],
        'material_claims':['收货地址: Cotton Street','衣身: 100% cotton']},
        'https://item.taobao.com/item.htm?id=1')
    assert result['material_claims'] == ['衣身: 100% cotton']
    assert 'Cotton Street' not in json.dumps(result)


def test_readiness_inspects_local_browser_only(tmp_path):
    c = client(tmp_path)
    c._browser = SimpleNamespace(is_connected=lambda:False)
    assert not c.is_ready
    c._browser = SimpleNamespace(is_connected=lambda:True)
    assert c.is_ready
    c.profile.release()
