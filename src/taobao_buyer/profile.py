"""Private profile directory and a process-wide Linux advisory lock."""

import fcntl
import os
from pathlib import Path

from .errors import SourcingError


DEFAULT_PROFILE = Path.home() / ".claude/.secrets/taobao-buyer-api"


class Profile:
    def __init__(self, path: Path | None = None):
        requested = Path(path or DEFAULT_PROFILE).expanduser().absolute()
        checkout = Path(__file__).resolve().parents[2]
        if requested.resolve().is_relative_to(checkout) or any(p.is_symlink() for p in (requested, *requested.parents)):
            raise SourcingError("invalid_profile", "Use a private profile outside the checkout.")
        self.path = requested
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.stat().st_uid != os.getuid():
            raise SourcingError("invalid_profile", "The profile must belong to the current user.")
        self.path.chmod(0o700)
        fd = os.open(self.path / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SourcingError("profile_busy", "The profile is in use. Use the running HTTP service.",
                                "wait", 409) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
