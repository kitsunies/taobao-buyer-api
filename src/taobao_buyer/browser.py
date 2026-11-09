"""Shared owner-operated browser. No bypasses, purchases or automatic retries."""

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import time
from urllib.parse import quote, urlsplit

from playwright.async_api import async_playwright, Error as BrowserError

from .dom import SNAPSHOT
from .errors import SourcingError
from .extract import extract_product, extract_search
from .images import normalize_image
from .policy import Policy
from .profile import Profile
from .urls import canonical_product_url

LOGIN_URL = "https://login.taobao.com/member/login.jhtml"
SEARCH_URL = "https://s.taobao.com/search"
PERSISTENT_DIR = "chrome"
# Keep the major version in step with the Playwright image in the Dockerfile.
DESKTOP_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
ALLOWED_DOMAINS = ("taobao.com", "tmall.com", "alicdn.com", "tbcdn.cn", "alipay.com",
                   "alibaba.com", "aliyuncs.com", "mmstat.com", "tanx.com")
VERIFY_MARKERS = ("请完成验证", "滑动验证", "拖动滑块", "访问受限", "操作太频繁",
                  "访问过于频繁", "unusual traffic", "verify you are human",
                  "FAIL_SYS_USER_VALIDATE", "RGV587_ERROR", "FAIL_SYS_TRAFFIC_LIMIT")


def access_error(status: int, url: str, text: str = "", *, login: bool = False):
    """Classify using local observations; do not expose any observed text."""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path.lower()
    if status == 429:
        return SourcingError("rate_limited", "Taobao requested a pause.", "wait", 429)
    if status == 403 or any(x in path for x in ("/punish", "/_____tmd_____", "/captcha")):
        return SourcingError("verification_required", "Human verification is required.", "verify", 403)
    if any(marker.lower() in text.lower() for marker in VERIFY_MARKERS):
        return SourcingError("verification_required", "Human verification is required.", "verify", 403)
    if status == 401 or (not login and host in {"login.taobao.com", "login.tmall.com", "login.m.taobao.com"}):
        return SourcingError("login_required", "Log in with the account owner.", "login", 401)
    if status >= 400:
        return SourcingError("upstream_access", "Taobao returned an access error.", "wait", 502)
    return None


class BuyerClient:
    def __init__(self, profile: Path | None = None):
        self.profile = Profile(profile)
        self.policy = None
        self._playwright = self._browser = self._context = self._login_page = None
        self._lock = asyncio.Lock()
        self._response_tasks = set()
        self._access_error = None
        self._logging_in = False
        self.ready = False

    @property
    def is_ready(self):
        return self.ready and self._browser is not None and self._browser.is_connected()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def start(self):
        if self.ready:
            return
        try:
            self.profile.acquire()
            self.policy = Policy(self.profile.path)
            self._playwright = await async_playwright().start()
            await self._open_context()
            self.ready = True
        except BaseException as exc:
            await self.close()
            if isinstance(exc, (SourcingError, asyncio.CancelledError)):
                raise
            raise SourcingError("browser_start", "Sandboxed Chromium could not start.",
                                "configure", 503) from None

    async def _open_context(self):
        directory = self.profile.path / PERSISTENT_DIR
        if directory.is_symlink():
            raise SourcingError("session_state", "Private browser profile is invalid.",
                                "clear_session", 503)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        # A hard kill can leave Chromium's singleton symlinks behind and block the next start.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            stale = directory / name
            if stale.is_symlink() or stale.is_file():
                stale.unlink(missing_ok=True)
        # The whole browser profile persists, so Taobao keeps trusting the device between
        # restarts. Cookies alone are not enough: item pages challenge a bare context.
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(directory), headless=True, channel="chromium", chromium_sandbox=True,
            args=["--disable-background-networking", "--disable-component-update",
                  "--disable-domain-reliability", "--metrics-recording-only"],
            locale="zh-CN", timezone_id="Australia/Sydney", user_agent=DESKTOP_USER_AGENT,
            viewport={"width": 1280, "height": 900}, accept_downloads=False,
            service_workers="block",
        )
        self._browser = self._context.browser
        await self._context.route("**/*", self._route)
        self._context.on("response", self._on_response)

    async def _route(self, route):
        parts = urlsplit(route.request.url)
        host = parts.hostname or ""
        if self._access_error is None and parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            await route.continue_()
        else:
            await route.abort()

    def _on_response(self, response):
        task = asyncio.create_task(self._observe_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_done)

    def _response_done(self, task):
        self._response_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._access_error = SourcingError("browser_observation", "Browser access could not be checked.", "configure", 503)

    async def _observe_response(self, response):
        host = urlsplit(response.url).hostname or ""
        if not any(host == d or host.endswith("." + d) for d in ("taobao.com", "tmall.com")):
            return
        try:
            is_login_page = (self._login_page is not None and response.request.frame.page == self._login_page)
            login = self._logging_in or is_login_page or response.request.resource_type != "document"
            error = access_error(response.status, response.url, login=login)
            if error is None and "mtop" in response.url:
                content_type = response.headers.get("content-type", "")
                length = response.headers.get("content-length", "")
                if ("json" in content_type or "javascript" in content_type) and (not length.isdigit() or int(length) < 2_000_000):
                    text = (await response.text())[:2_000_000]
                    error = access_error(200, response.url, text, login=login)
                    if error is None and any(x in text for x in ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXPIRED")):
                        error = SourcingError("login_required", "The session expired.", "login", 401)
            if error:
                self._block(error, response.headers.get("retry-after"))
        except BrowserError:
            return

    def _block(self, error, retry_after=None):
        if self._access_error is not None:
            return
        self._access_error = error
        if error.action in {"verify", "wait"}:
            try:
                error.retry_after = self.policy.cooldown(retry_after)
            except SourcingError as state_error:
                self._access_error = state_error
            except OSError:
                self._access_error = SourcingError("private_state", "Private state could not be saved.", "configure", 503)

    async def _check_page(self, page, *, login=False):
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks))
        if self._access_error:
            raise self._access_error
        for frame in page.frames:
            try:
                text = await frame.locator("body").inner_text(timeout=3000)
            except BrowserError:
                continue
            error = access_error(200, frame.url, text, login=login)
            if error:
                self._block(error)
                raise error

    async def close(self):
        self.ready = False
        try:
            if self._context:
                await self._context.close()
        finally:
            try:
                if self._browser:
                    await self._browser.close()
                if self._playwright:
                    await self._playwright.stop()
            finally:
                self._browser = self._context = self._playwright = self._login_page = None
                self.profile.release()

    async def _authenticated(self):
        cookies = await self._context.cookies("https://www.taobao.com")
        names = {x["name"] for x in cookies if x.get("expires", -1) < 0 or x["expires"] > time.time()}
        return "cookie17" in names or {"unb", "lgc"}.issubset(names)

    async def session_status(self):
        async with self._operation():
            authenticated = await self._authenticated()
            return {"state": "authenticated" if authenticated else "login_required",
                    "restored": (self.profile.path / PERSISTENT_DIR).exists(),
                    "limits": self.policy.summary()}

    async def session_clear(self):
        async with self._operation():
            await self._context.close()
            self._context = self._login_page = None
            directory = self.profile.path / PERSISTENT_DIR
            if directory.is_symlink():
                raise SourcingError("session_state", "Private browser profile is invalid.",
                                    "clear_session", 503)
            if directory.exists():
                shutil.rmtree(directory)
            await self._open_context()
            return {"state": "cleared", "limits": self.policy.summary()}

    @asynccontextmanager
    async def _operation(self, *, sourcing=False, network=False):
        if self._lock.locked():
            raise SourcingError("busy", "Another browser operation is active.", "wait", 409)
        async with self._lock:
            if not self.ready:
                await self.start()
            if network:
                self.policy.check()
                self._access_error = None
            if sourcing:
                if not await self._authenticated():
                    raise SourcingError("login_required", "Log in with the account owner.", "login", 401)
                self.policy.record()
            try:
                yield
            except OSError:
                raise SourcingError("private_state", "Private state could not be saved.", "configure", 503) from None
            except BrowserError:
                if self._access_error:
                    raise self._access_error from None
                raise SourcingError("browser_operation", "The browser operation did not complete.",
                                    "retry_manually", 502) from None

    async def login_start(self):
        async with self._operation(network=True):
            self._logging_in = True
            try:
                if await self._authenticated():
                    return {"state": "authenticated"}
                if self._login_page is None or self._login_page.is_closed():
                    self._login_page = await self._context.new_page()
                    await self._login_page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                return await self._login_result()
            finally:
                self._logging_in = False

    async def _login_result(self):
        page = self._login_page
        await self._check_page(page, login=True)
        if await self._authenticated():
            await page.close()
            self._login_page = None
            return {"state": "authenticated"}
        for frame in page.frames:
            try:
                body = await frame.locator("body").inner_text(timeout=3000)
            except BrowserError:
                body = ""
            if any(x in body for x in ("二维码已失效", "二维码失效", "二维码已过期")):
                return {"state": "expired"}
            if any(x in body for x in ("扫描成功", "扫码成功", "请在手机上确认")):
                return {"state": "scanned"}
            try:
                qr = frame.locator(".qrcode-img canvas, .qrcode-main canvas, canvas")
                if await qr.count():
                    await qr.first.wait_for(state="visible", timeout=5000)
                    png = await qr.first.screenshot(type="png")
                    return {"state": "waiting", "qr_png_base64": base64.b64encode(png).decode("ascii")}
            except BrowserError:
                pass
            try:
                qr_image = frame.locator('.qrcode-img img, img[alt*="二维码"]')
                if await qr_image.count() and await qr_image.first.is_visible():
                    png = await qr_image.first.screenshot(type="png", timeout=5000)
                    return {"state": "waiting", "qr_png_base64": base64.b64encode(png).decode("ascii")}
            except BrowserError:
                pass
        raise SourcingError("login_layout", "The login QR could not be located.", "update_required", 502)

    async def login_status(self):
        async with self._operation():
            if self._access_error:
                raise self._access_error
            if self._login_page is None:
                return {"state": "authenticated" if await self._authenticated() else "login_required"}
            return await self._login_result()

    async def login_refresh(self):
        async with self._operation(network=True):
            if self._login_page is None:
                raise SourcingError("login_required", "Start QR login first.", "login", 409)
            self._logging_in = True
            try:
                await self._check_page(self._login_page, login=True)
                for frame in self._login_page.frames:
                    refresh = frame.get_by_text("刷新", exact=False)
                    if await refresh.count() and await refresh.first.is_visible():
                        await refresh.first.click(timeout=5000)
                        return await self._login_result()
                raise SourcingError("qr_refresh", "A QR refresh control is not visible.", "wait", 409)
            finally:
                self._logging_in = False

    async def _load(self, page, url):
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response:
            error = access_error(response.status, page.url)
            if error:
                self._block(error, response.headers.get("retry-after"))
        await self._check_page(page)

    async def _snapshot(self, page, *, product=False):
        selector = ('[class*="mainTitle"], [class*="MainTitle"], h1, #J_Title' if product else
                    'a[href*="item.taobao.com"],a[href*="pcdetail.taobao.com"],a[href*="detail.tmall.com"]')
        try:
            # A broad selector can match a hidden sticky-header copy first; wait for any
            # visible match rather than the first element in DOM order.
            await page.locator(selector + " >> visible=true").first.wait_for(state="visible", timeout=15000)
        except BrowserError:
            await self._check_page(page)
            raise SourcingError("layout_changed", "Expected product content was not found.",
                                "update_required", 502) from None
        await self._check_page(page)
        return await page.evaluate(SNAPSHOT)

    async def product(self, url: str):
        url = canonical_product_url(url)
        async with self._operation(sourcing=True, network=True):
            page = await self._context.new_page()
            try:
                await self._load(page, url)
                await self._snapshot(page, product=True)
                details = page.get_by_text("图文详情", exact=True)
                if await details.count() and await details.first.is_visible():
                    await details.first.click(timeout=5000)
                    await self._check_page(page)
                # A bounded scroll reveals lazy detail images without paging or purchasing.
                for _ in range(3):
                    await page.mouse.wheel(0, 900)
                    await page.wait_for_timeout(400)
                    await self._check_page(page)
                result = extract_product(await page.evaluate(SNAPSHOT), url)
                if not result.get("title"):
                    raise SourcingError("layout_changed", "Product title was not found.", "update_required", 502)
                return result
            finally:
                await page.close()

    async def search(self, text: str):
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise SourcingError("invalid_search", "Search text must contain 1 to 200 characters.")
        async with self._operation(sourcing=True, network=True):
            page = await self._context.new_page()
            try:
                await self._load(page, SEARCH_URL + "?q=" + quote(text.strip()))
                result = extract_search(await self._snapshot(page))
                return result
            finally:
                await page.close()

    async def search_image(self, data: bytes):
        data, mime = normalize_image(data)
        async with self._operation(sourcing=True, network=True):
            page = await self._context.new_page()
            before = set(self._context.pages)
            try:
                await self._load(page, SEARCH_URL)
                initial_url = page.url
                upload = page.locator('input[type="file"]')
                await upload.first.wait_for(state="attached", timeout=10000)
                await upload.first.set_input_files({"name": "reference.png" if mime == "image/png" else "reference.jpg",
                                                    "mimeType": mime, "buffer": data})
                # Taobao may submit automatically or wait for the preview panel's
                # "搜索" button; some builds open results in a new tab. Handle all three.
                target = page
                submitted = False
                for _ in range(60):
                    await self._check_page(page)
                    new_pages = [p for p in self._context.pages if p not in before]
                    if new_pages:
                        target = new_pages[0]
                        break
                    if page.url != initial_url:
                        break
                    if not submitted:
                        submit = page.locator(".image-search-context-wrapper .upload-button")
                        try:
                            if await submit.count() and await submit.first.is_visible():
                                await submit.first.click(timeout=5000)
                                submitted = True
                                continue
                        except BrowserError:
                            pass
                    await page.wait_for_timeout(250)
                if target == page and page.url == initial_url:
                    raise SourcingError("image_search_incomplete", "Taobao did not open image results.", "retry_manually", 502)
                result = extract_search(await self._snapshot(target))
                return result
            finally:
                for item in list(self._context.pages):
                    if item not in before or item == page:
                        await item.close()
