# taobao-buyer-api

A Linux Python library, JSON CLI and private HTTP service for owner-operated Taobao sourcing. It reads products and the first search-results page through the account owner's browser session. It cannot purchase items, bypass verification, rotate identities or guarantee that an account will avoid restrictions.

The HTTP service is a separate process using the same async browser client. One account and one active browser operation are allowed per instance. Local processes lock the private profile; remote clients use HTTP.

## Install

Python 3.12 or later:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
playwright install chromium
```

Playwright is pinned to the tested version 1.64.0. Chromium runs with its sandbox enabled. Linux must permit its user namespaces. The [sandbox profiles](sandbox/README.md) describe the scoped AppArmor and seccomp policies; startup fails when the sandbox cannot be verified.

## CLI

```sh
taobao-buyer login
taobao-buyer session status
taobao-buyer product 'https://item.taobao.com/item.htm?id=123'
taobao-buyer search '纯棉 T恤'
taobao-buyer search-image ./reference.webp
taobao-buyer session clear
```

Data and structured errors go to JSON stdout. Login progress and its ASCII QR go to stderr. Scan using the Taobao app; enter `r` to refresh the QR in the same browser session, or `q` to cancel. Expiry does not trigger automatic refresh.

The default profile is `~/.claude/.secrets/taobao-buyer-api/`. Override it with `taobao-buyer --profile /private/path COMMAND`. Profiles must be outside the installed source tree and owned by the process user. Directories are mode 0700 and saved browser state is mode 0600. Cookies, IndexedDB and local storage in this directory are account secrets. Session clear removes saved authentication, while preserving sourcing budgets and cooldowns. Status checks local cookie expiry; remote session revocation is detected on access.

## Python

```python
import asyncio
from taobao_buyer import BuyerClient, SourcingError

async def main():
    async with BuyerClient() as buyer:
        try:
            observation = await buyer.product(
                'https://item.taobao.com/item.htm?id=123'
            )
            print(observation)
        except SourcingError as error:
            print(error.as_dict())

asyncio.run(main())
```

Async methods: `session_status`, `session_clear`, `login_start`, `login_status`, `login_refresh`, `product(url)`, `search(text)` and `search_image(bytes)`. Use `async with`, or call `start` and `close`. Concurrent operations return `busy`; a second process receives `profile_busy`.

## HTTP

Create a random bearer token in a protected file, without putting it into command history or an environment variable:

```sh
python3 - <<'PY'
import os, secrets
from pathlib import Path
folder = Path.home() / '.claude/.secrets/taobao-buyer-api'
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
folder.chmod(0o700)
fd = os.open(folder / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(secrets.token_urlsafe(48))
PY
taobao-buyer serve --host 127.0.0.1 --port 8787
```

The server reads the file once at startup. Pass `--token-file /private/token` to override its location. It must be a regular non-symlink file, owned by the current user or root, without world permissions or group write access. Root-owned group-readable Kubernetes Secret files are supported with a matching effective group. Rotate the token by replacing the Secret/file and restarting the service.

Send `Authorization: Bearer <token>` on every `/v1` request. Avoid embedding the token in shell arguments, URLs or logs. This Python example keeps it in memory:

```python
from pathlib import Path
import httpx

token = (Path.home() / '.claude/.secrets/taobao-buyer-api/token').read_text().strip()
with httpx.Client(base_url='http://127.0.0.1:8787',
                  headers={'Authorization': 'Bearer ' + token}) as client:
    print(client.get('/v1/session').json())
```

`httpx` is an optional HTTP caller, also included in test dependencies.

| Method | Endpoint | JSON body |
| --- | --- | --- |
| GET | `/v1/session` | none |
| DELETE | `/v1/session` | none |
| POST | `/v1/session/login` | none |
| GET | `/v1/session/login` | none |
| POST | `/v1/session/login/refresh` | none |
| POST | `/v1/product` | `{"url":"https://item.taobao.com/item.htm?id=123"}` |
| POST | `/v1/search` | `{"text":"纯棉 T恤"}` |
| POST | `/v1/search-image` | `{"image_base64":"<base64 image bytes>"}` |

Login responses contain a `state` and, while waiting, `qr_png_base64`. Treat a QR as sensitive. Responses use `Cache-Control: no-store`. Images cannot be supplied by remote URL. JSON bodies are limited to 15 MiB before parsing. Image input is limited to 10 MiB, 16 million pixels and 8192 pixels per side. JPEG, PNG and WebP are decoded and re-encoded as JPEG or PNG without metadata; animated files are rejected.

`/healthz` and `/readyz` need no token and inspect only the local process/browser. They never contact Taobao. API documentation, request access logs, application telemetry, browser traces and general screenshots are disabled. Only requested login QR pixels are returned. Browser control uses a local pipe, without a TCP debugging port. Use a trusted private network or an encrypted tunnel for HTTP.

## Observations and evidence

Product observations include canonical `url`, UTC `observed_at`, `title`, `price`, `variants`, public `seller`, `attributes`, `gallery`, `detail_images`, `material_claims` and `size_chart_images`. Search returns `page: 1` and `products`. Unknown or absent fields remain empty or null; nothing infers measurements or fabric percentages.

Prices preserve the displayed text and nearby qualifications, with `checkout_price_verified: false`. Coupon, membership, deposit, subsidy and starting-price text does not establish a checkout price. Fabric claims retain component-specific wording. A body and collar can have different materials. Detailed evidence may exist only in images: this tool does not OCR charts or material labels. `size_chart_images` contains explicitly labelled chart images; unlabelled charts remain in `detail_images`. Preserve those sources for downstream inspection. Bounded scrolling can miss details on unusually long pages.

Only supported Taobao/Tmall product URLs, public shop links and allowlisted Alibaba image-CDN URLs are exported. Query tracking parameters, fragments and URL credentials are removed or rejected. Unknown browser state, HTML, account identifiers, cookies, tokens, addresses and headers are not exported. Scoped DOM readers avoid account navigation. Real account captures must never be committed as fixtures.

## Limits and access failures

Conservative defaults are 30 seconds between sourcing operations, 20 operations per rolling hour and 100 per rolling day. These are product defaults, not verified Taobao limits. Budgets and cooldowns survive browser restarts and login clear.

HTTP 403/429, login redirects and visible verification pages are detected, including challenges returned with HTTP 200 and known Taobao API challenge responses. Detection stops further browser requests. No challenge or rate limit is automatically retried. A numeric or date-form `Retry-After` is respected; an unusable or absent value imposes 30 minutes. Follow the structured error's `action` (`login`, `verify`, `wait`, or an operator action), and retry only deliberately. A human verification challenge may require waiting and operator review outside the service.

Taobao can change page structure or access policy. Missing expected content returns an extraction/layout error rather than fabricated product information. Sustained operation, actual rate thresholds and live 429 recovery remain unverified.

## Test and deploy

```sh
pip install -r requirements-test.lock
pytest -q
```

Tests use synthetic fixtures without credentials or live Taobao requests. The real-browser integration check runs in the container with networking disabled. It verifies sandbox status, DOM extraction, cookie/local-storage restoration and native image-upload results. The CI workflow builds the image, runs both sandbox dialects under `--network none`, and publishes an immutable digest only for a reviewed revision dispatched with `publish=true`.

MIT licensed. AI analysis, business preferences and Discord integrations belong outside this project.
