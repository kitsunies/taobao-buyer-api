# Focused checks

- Data/privacy/policy: `.venv/bin/pytest tests/test_data.py -q`
- HTTP/CLI: `.venv/bin/pytest tests/test_interfaces.py -q`
- Browser guards: `.venv/bin/pytest tests/test_browser.py -q`
- Full synthetic suite: `.venv/bin/pytest -q`
- Real offline browser: run `python scripts/offline_check.py` in the documented sandbox container with `--network none`.

Never capture a real account in fixtures, logs, screenshots or traces. Session state and token files belong outside this checkout. Clearing login must preserve policy cooldowns. No sourcing-operation retries on challenges or rate limits.

Docker/containerd need scoped userns AppArmor plus chroot-enabled seccomp with all capabilities dropped; `chrome://sandbox` requires the full Chromium channel.
