"""JSON commands with human login progress on stderr."""

import argparse
import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
import select
import sys

from PIL import Image, ImageOps

from .errors import SourcingError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SourcingError("invalid_input", "Invalid command arguments. Use --help.",
                            "correct_input")


def parser() -> argparse.ArgumentParser:
    result = Parser(prog="taobao-buyer")
    result.add_argument("--profile", type=Path, help="Private browser profile directory")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Display a QR login; r refreshes and q cancels")
    sessions = commands.add_parser("session").add_subparsers(dest="session_command", required=True)
    sessions.add_parser("status")
    sessions.add_parser("clear")
    commands.add_parser("product").add_argument("url")
    commands.add_parser("search").add_argument("text")
    commands.add_parser("search-image").add_argument("file", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--token-file", type=Path)
    return result


def ascii_qr(png_base64: str) -> str:
    try:
        data = base64.b64decode(png_base64, validate=True)
        if len(data) > 1024 * 1024:
            raise ValueError
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG" or source.width > 2048 or source.height > 2048:
                raise ValueError
            image = source.convert("L").point(lambda x: 255 if x >= 128 else 0)
        bbox = ImageOps.invert(image).getbbox()
        if not bbox:
            raise ValueError
        image = image.crop(bbox)
        first_run = 0
        while first_run < image.width and image.getpixel((first_run, 0)) == 0:
            first_run += 1
        # The top-left QR finder is seven modules wide, independent of CSS scaling.
        modules = round(image.width * 7 / first_run)
        if not 21 <= modules <= 177 or (modules - 21) % 4 or abs(image.width - image.height) > 2:
            raise ValueError
        image = image.resize((modules, modules), Image.Resampling.NEAREST)
        image = ImageOps.expand(image, border=4, fill=255)
        return "\n".join("".join("##" if image.getpixel((x, y)) == 0 else "  "
                                  for x in range(image.width)) for y in range(image.height))
    except (OSError, ValueError, ZeroDivisionError):
        raise SourcingError("qr_unavailable", "The QR image could not be displayed.",
                            "refresh_login") from None


async def login(client) -> dict:
    result = await client.login_start()
    print("Scan with Taobao. Enter r to refresh or q to cancel.", file=sys.stderr)
    previous_state = None
    previous_qr = None
    while True:
        state = result.get("state", "unknown")
        if state != previous_state:
            print(f"Login: {state}", file=sys.stderr)
            previous_state = state
        if result.get("qr_png_base64") and result["qr_png_base64"] != previous_qr:
            print(ascii_qr(result["qr_png_base64"]), file=sys.stderr)
            previous_qr = result["qr_png_base64"]
        if state == "authenticated":
            return {"state": state}
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            line = sys.stdin.readline()
            if not line or line.strip().lower() == "q":
                return {"state": "cancelled"}
            if line.strip().lower() == "r":
                result = await client.login_refresh()
                continue
        await asyncio.sleep(1)
        result = await client.login_status()


async def run_command(args, client=None) -> dict:
    if client is None:
        from .browser import BuyerClient
        client = BuyerClient(profile=args.profile)
    async with client:
        if args.command == "login":
            return await login(client)
        if args.command == "session":
            if args.session_command == "clear":
                return await client.session_clear()
            return await client.session_status()
        if args.command == "product":
            return await client.product(args.url)
        if args.command == "search":
            return await client.search(args.text)
        if args.command == "search-image":
            from .api import MAX_IMAGE_BYTES
            try:
                with args.file.open("rb") as image:
                    data = image.read(MAX_IMAGE_BYTES + 1)
            except OSError:
                raise SourcingError("invalid_image", "Image file could not be read.",
                                    "correct_input") from None
            if len(data) > MAX_IMAGE_BYTES:
                raise SourcingError("input_too_large", "Image is too large.", "reduce_input", 413)
            return await client.search_image(data)
    raise SourcingError("invalid_input", "Unknown command.", "correct_input")


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise SourcingError("invalid_input", "Invalid server port.", "correct_input")
            import uvicorn
            from .api import create_app
            from .browser import BuyerClient
            app = create_app(BuyerClient(profile=args.profile), token_file=args.token_file)
            uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                        log_config=None, log_level="critical")
            return 0
        print(json.dumps(asyncio.run(run_command(args)), ensure_ascii=False))
        return 0
    except SourcingError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "cancelled", "message": "Operation cancelled.",
                                   "action": "none"}}))
        return 130
    except Exception:
        print(json.dumps({"error": {"code": "internal_error", "message": "The operation failed.",
                                   "action": "retry_manually"}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
