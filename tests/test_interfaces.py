import asyncio
import base64
from io import BytesIO, StringIO
import json

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from taobao_buyer import api, cli
from taobao_buyer.errors import SourcingError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def start(self):
        self.calls.append("start")

    async def close(self):
        self.calls.append("close")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def session_status(self):
        self.calls.append("session_status")
        return {"authenticated": False}

    async def session_clear(self):
        self.calls.append("session_clear")
        return {"authenticated": False}

    async def login_start(self):
        self.calls.append("login_start")
        return {"state": "waiting", "qr_png_base64": "synthetic"}

    async def login_status(self):
        self.calls.append("login_status")
        return {"state": "authenticated"}

    async def login_refresh(self):
        self.calls.append("login_refresh")
        return {"state": "waiting", "qr_png_base64": "refreshed"}

    async def product(self, url):
        self.calls.append(("product", url))
        if self.failure:
            raise self.failure
        return {"url": "https://item.taobao.com/item.htm?id=123"}

    async def search(self, text):
        self.calls.append(("search", text))
        return {"items": []}

    async def search_image(self, data):
        self.calls.append(("search_image", data))
        return {"items": []}


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("a" * 40)
    path.chmod(0o600)
    return path


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def client(fake, token_file):
    with TestClient(api.create_app(fake, token_file), headers={"Authorization": "Bearer " + "a" * 40}) as http:
        yield http


def test_health_is_local_and_lifespan_closes(fake, token_file):
    with TestClient(api.create_app(fake, token_file)) as http:
        assert http.get("/healthz").json() == {"status": "ok"}
        assert http.get("/readyz").json() == {"ready": True}
        assert fake.calls == ["start"]
    assert fake.calls == ["start", "close"]


def test_auth_and_docs(fake, token_file):
    with TestClient(api.create_app(fake, token_file)) as http:
        for header in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
            response = http.get("/v1/session", headers=header)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.headers["cache-control"] == "no-store"
        assert fake.calls == ["start"]
        assert http.get("/docs", headers={"Authorization": "Bearer " + "a" * 40}).status_code == 404


def test_endpoints_and_qr_no_store(client, fake):
    assert client.get("/v1/session").json() == {"authenticated": False}
    assert client.delete("/v1/session").status_code == 200
    for method, path, operation in [("post", "/v1/session/login", "login_start"),
                                    ("post", "/v1/session/login/refresh", "login_refresh"),
                                    ("get", "/v1/session/login", "login_status")]:
        response = getattr(client, method)(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert operation in fake.calls
    assert client.post("/v1/product", json={"url": "https://item.taobao.com/item.htm?id=123"}).status_code == 200
    assert client.post("/v1/search", json={"text": "棉"}).status_code == 200
    assert client.post("/v1/search-image", json={"image_base64": "aW1hZ2U="}).status_code == 200
    assert ("search_image", b"image") in fake.calls


@pytest.mark.parametrize("payload", [{"url": []}, {"url": "secret", "token": "private"},
                                      {"url": "x" * 2049}])
def test_validation_does_not_echo_private_input(client, payload, caplog):
    response = client.post("/v1/product", json=payload)
    assert response.status_code == 422
    assert "secret" not in response.text and "private" not in response.text
    assert "secret" not in caplog.text and "private" not in caplog.text


def test_invalid_search_and_base64(client, fake):
    before = len(fake.calls)
    assert client.post("/v1/search", json={"text": "   "}).status_code == 422
    for invalid in ("https://example.test/private.png", "@@@@", "aW1hZ2U=\n"):
        assert client.post("/v1/search-image", json={"image_base64": invalid}).status_code == 422
    assert len(fake.calls) == before


def test_body_limit_content_length_and_chunked(client, fake, monkeypatch):
    monkeypatch.setattr(api, "MAX_BODY_BYTES", 64)
    before = len(fake.calls)
    assert client.post("/v1/search", content=b"x" * 65).status_code == 413
    assert client.post("/v1/search", content=iter([b"x" * 40, b"x" * 40])).status_code == 413
    assert len(fake.calls) == before


def test_unexpected_exception_suppressed(client, fake, caplog):
    fake.failure = RuntimeError("cookie=private-account@example.test; address secret")
    response = client.post("/v1/product", json={"url": "synthetic"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private-account" not in response.text + caplog.text
    assert "address secret" not in response.text + caplog.text


def test_structured_wait_error(client, fake):
    fake.failure = SourcingError("cooldown", "Wait before sourcing.", "wait", 429, 1800)
    response = client.post("/v1/product", json={"url": "synthetic"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1800"
    assert response.json()["error"]["action"] == "wait"


def test_private_token_file(token_file, tmp_path):
    assert api.read_token(token_file) == "a" * 40
    token_file.chmod(0o644)
    with pytest.raises(SourcingError, match="protected bearer"):
        api.read_token(token_file)
    token_file.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(token_file)
    with pytest.raises(SourcingError):
        api.read_token(link)
    token_file.write_text("short")
    with pytest.raises(SourcingError):
        api.read_token(token_file)


def test_telemetry_disabled(fake, token_file):
    app = api.create_app(fake, token_file)
    assert all(app._telemetry[key] is False for key in
               ("tracing", "metrics", "logs", "operation_spans", "auto_configure"))


def test_cli_commands_are_json(capsys, monkeypatch):
    async def command(args):
        return {"items": [], "query": args.text}
    monkeypatch.setattr(cli, "run_command", command)
    assert cli.main(["search", "棉"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"items": [], "query": "棉"}
    assert output.err == ""
    assert cli.main(["unknown"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_cli_private_exception_not_printed(capsys, monkeypatch):
    async def command(args):
        raise RuntimeError("account secret")
    monkeypatch.setattr(cli, "run_command", command)
    assert cli.main(["session", "status"]) == 1
    result = capsys.readouterr()
    assert "account secret" not in result.out + result.err


def test_cli_image_and_context(fake, tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"synthetic")
    args = cli.parser().parse_args(["search-image", str(path)])
    assert asyncio.run(cli.run_command(args, fake)) == {"items": []}
    assert fake.calls == ["start", ("search_image", b"synthetic"), "close"]


def test_ascii_qr_preserves_modules_and_quiet_zone():
    image = Image.new("L", (29, 29), 255)
    draw = ImageDraw.Draw(image)
    for x, y in ((4, 4), (18, 4), (4, 18)):
        draw.rectangle((x, y, x + 6, y + 6), fill=0)
        draw.rectangle((x + 1, y + 1, x + 5, y + 5), fill=255)
        draw.rectangle((x + 2, y + 2, x + 4, y + 4), fill=0)
    image = image.resize((145, 145), Image.Resampling.NEAREST)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    output = cli.ascii_qr(base64.b64encode(buffer.getvalue()).decode())
    lines = output.splitlines()
    assert len(lines) == 29 and all(len(line) == 58 for line in lines)
    assert lines[4][8:22] == "##" * 7
    assert all(line == " " * 58 for line in lines[:4])


def test_login_manual_refresh_keeps_client(fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ascii_qr", lambda image: image)
    monkeypatch.setattr(cli.sys, "stdin", StringIO("r\nq\n"))
    monkeypatch.setattr(cli.select, "select", lambda *args: ([cli.sys.stdin], [], []))
    result = asyncio.run(cli.login(fake))
    assert result == {"state": "cancelled"}
    assert fake.calls == ["login_start", "login_refresh"]
    assert "refreshed" in capsys.readouterr().err
