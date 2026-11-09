"""Offline, fail-closed proof of Chromium's renderer sandbox inside this runtime."""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def check() -> dict:
    if os.geteuid() == 0:
        raise RuntimeError("Sandbox verification requires a non-root user")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(chromium_sandbox=True, channel="chromium")
        try:
            page = await browser.new_page()
            await page.goto("chrome://sandbox")
            status = " ".join((await page.locator("body").inner_text()).split())
            if "Seccomp-BPF sandbox Yes" not in status:
                raise RuntimeError("Chromium Seccomp-BPF sandbox is not enabled")
            if not any(f"{name} Yes" in status for name in ("PID namespaces", "SUID sandbox")):
                raise RuntimeError("Chromium layer-one sandbox is not enabled")
            command_lines = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    command = (entry / "cmdline").read_bytes().split(b"\0")
                    if command and b"/chrome" in command[0]:
                        command_lines.append(command)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            if not command_lines:
                raise RuntimeError("Cannot inspect Chromium launch arguments")
            for command in command_lines:
                if any(arg in (b"--no-sandbox", b"--disable-setuid-sandbox") or
                       arg.startswith(b"--remote-debugging-port") for arg in command):
                    raise RuntimeError("Unsafe Chromium launch arguments")
            return {"sandbox": "enabled", "uid": os.geteuid(), "offline": True}
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(check())), flush=True)
    except Exception:
        raise SystemExit("Chromium sandbox verification failed; refusing to start") from None
