"""Conservative sourcing budgets, persisted independently of browser sessions."""

import json
import math
import os
import tempfile
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .errors import SourcingError


def retry_after_seconds(value: str | None, now: float | None = None) -> int:
    now = time.time() if now is None else now
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdigit():
            try:
                seconds = int(value)
                if seconds <= 253402300799:
                    return max(1, seconds)
            except (ValueError, OverflowError):
                pass
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                seconds = parsed.astimezone(timezone.utc).timestamp() - now
                if seconds > 0:
                    return math.ceil(seconds)
        except (TypeError, ValueError, OverflowError):
            pass
    return 1800


class Policy:
    def __init__(self, profile_path: Path):
        self.path = Path(profile_path) / "policy.json"

    def _load(self, now):
        if self.path.is_symlink():
            raise SourcingError("policy_state_invalid", "The local policy state needs operator review.", "operator", 503)
        try:
            with self.path.open() as source:
                data = json.load(source)
            operations, cooldown = data["operations"], data["cooldown_until"]
            if (not isinstance(operations, list) or len(operations) > 10000
                    or not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool)
                    or not math.isfinite(cooldown) or cooldown < 0
                    or any(not isinstance(t, (int, float)) or isinstance(t, bool)
                           or not math.isfinite(t) or t < 0 for t in operations)):
                raise ValueError
            return {"operations": sorted(t for t in operations if t > now - 86400),
                    "cooldown_until": cooldown}
        except FileNotFoundError:
            return {"operations": [], "cooldown_until": 0}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise SourcingError("policy_state_invalid", "The local policy state needs operator review.", "operator", 503) from None

    def _save(self, data):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".policy-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as destination:
                json.dump(data, destination, allow_nan=False)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _wait(data, now):
        operations = data["operations"]
        waits = [data["cooldown_until"] - now]
        if operations:
            waits.append(operations[-1] + 30 - now)
        hourly = [t for t in operations if t > now - 3600]
        if len(hourly) >= 20:
            waits.append(hourly[-20] + 3600 - now)
        if len(operations) >= 100:
            waits.append(operations[-100] + 86400 - now)
        return max(0, math.ceil(max(waits)))

    def check(self, now: float | None = None):
        now = time.time() if now is None else now
        data = self._load(now)
        wait = self._wait(data, now)
        if wait:
            raise SourcingError("cooldown" if data["cooldown_until"] > now else "sourcing_budget",
                                "Wait before the next sourcing operation.", "wait", 429, wait)

    def record(self, now: float | None = None):
        now = time.time() if now is None else now
        self.check(now)
        data = self._load(now)
        data["operations"].append(now)
        self._save(data)

    def cooldown(self, retry_after: str | None = None, now: float | None = None):
        now = time.time() if now is None else now
        data = self._load(now)
        data["cooldown_until"] = max(data["cooldown_until"], now + retry_after_seconds(retry_after, now))
        self._save(data)
        return self._wait(data, now)

    def summary(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        data = self._load(now)
        return {"minimum_interval_seconds": 30, "hourly_limit": 20, "daily_limit": 100,
                "operations_last_hour": sum(t > now - 3600 for t in data["operations"]),
                "operations_last_day": len(data["operations"]), "retry_after": self._wait(data, now)}
