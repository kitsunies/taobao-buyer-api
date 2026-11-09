"""Private HTTP surface. Health probes never run a sourcing operation."""

import base64
import binascii
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import stat

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .errors import SourcingError

MAX_BODY_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TOKEN_FILE = Path.home() / ".claude/.secrets/taobao-buyer-api/token"


def read_token(path: Path) -> str:
    """Permit private user files and root-owned, group-readable pod Secrets."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as file:
            info = os.fstat(file.fileno())
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                    or mode & 0o137 or (mode & 0o040 and info.st_gid != os.getegid())):
                raise ValueError
            token = file.read(4097).strip()
        if not 32 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise ValueError
        return token
    except (OSError, UnicodeError, ValueError):
        raise SourcingError("token_file", "A protected bearer token file is required.",
                            "configure", 503) from None


def error_response(error: SourcingError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(error.as_dict(), status_code=error.status, headers=headers)


class RequestBoundary:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path not in {"/healthz", "/readyz"}:
            authorization = [value for key, value in scope["headers"] if key == b"authorization"]
            expected = ("Bearer " + getattr(self.state, "token", "")).encode("ascii")
            if (len(authorization) != 1 or not getattr(self.state, "token", None)
                    or not hmac.compare_digest(authorization[0], expected)):
                await error_response(SourcingError("unauthorized", "A bearer token is required.",
                                                   "authenticate", 401))(scope, receive, send)
                return
        body = bytearray()
        try:
            lengths = [value for key, value in scope["headers"] if key == b"content-length"]
            if lengths and (len(lengths) != 1 or int(lengths[0]) < 0):
                raise ValueError
            if lengths and int(lengths[0]) > MAX_BODY_BYTES:
                raise OverflowError
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY_BYTES:
                    raise OverflowError
                if not message.get("more_body", False):
                    break
        except OverflowError:
            await error_response(SourcingError("input_too_large", "Request body is too large.",
                                               "reduce_input", 413))(scope, receive, send)
            return
        except ValueError:
            await error_response(SourcingError("invalid_request", "Invalid request framing.",
                                               "correct_input", 400))(scope, receive, send)
            return

        async def body_receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        started = False

        async def private_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [(k, v) for k, v in message["headers"]
                                      if k.lower() != b"cache-control"]
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        try:
            await self.app(scope, body_receive, private_send)
        except Exception:
            # Browser exceptions can include cookies, account data or private URLs.
            if not started:
                await error_response(SourcingError("internal_error", "The operation failed.",
                                                   "retry_manually", 500))(scope, receive, send)


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=2048)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=200)


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image_base64: str = Field(min_length=1, max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3))


def create_app(client=None, token_file: Path | str | None = None) -> FastAPI:
    if client is None:
        from .browser import BuyerClient
        client = BuyerClient()

    @asynccontextmanager
    async def lifespan(app):
        app.state.token = read_token(Path(token_file) if token_file else DEFAULT_TOKEN_FILE)
        app.state.ready = False
        try:
            await client.start()
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            await client.close()
            app.state.token = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry={"tracing": False, "metrics": False, "logs": False,
                             "operation_spans": False, "auto_configure": False})
    app.state.client = client
    app.state.ready = False
    app.add_middleware(RequestBoundary, state=app.state)

    @app.exception_handler(SourcingError)
    async def sourcing_error(request: Request, error: SourcingError):
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        return error_response(SourcingError("invalid_input", "Request input is invalid.",
                                            "correct_input", 422))

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        local_ready = app.state.ready and getattr(client, "is_ready", True)
        return JSONResponse({"ready": local_ready}, status_code=200 if local_ready else 503)

    @app.get("/v1/session")
    async def session():
        return await client.session_status()

    @app.delete("/v1/session")
    async def clear_session():
        return await client.session_clear()

    @app.post("/v1/session/login")
    async def login_start():
        return await client.login_start()

    @app.post("/v1/session/login/refresh")
    async def login_refresh():
        return await client.login_refresh()

    @app.get("/v1/session/login")
    async def login_status():
        return await client.login_status()

    @app.post("/v1/product")
    async def product(data: ProductInput):
        return await client.product(data.url)

    @app.post("/v1/search")
    async def search(data: SearchInput):
        if not data.text.strip():
            raise SourcingError("invalid_input", "Search text is required.", "correct_input", 422)
        return await client.search(data.text)

    @app.post("/v1/search-image")
    async def search_image(data: ImageInput):
        try:
            image = base64.b64decode(data.image_base64, validate=True)
        except (ValueError, binascii.Error):
            raise SourcingError("invalid_image", "Image must be valid base64.",
                                "correct_input", 422) from None
        if len(image) > MAX_IMAGE_BYTES:
            raise SourcingError("input_too_large", "Image is too large.", "reduce_input", 413)
        return await client.search_image(image)

    return app
