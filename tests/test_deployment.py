"""Guard the runtime profile's privilege boundary without contacting Taobao."""

import json
from pathlib import Path


def test_kubernetes_seccomp_is_resolved_and_keeps_privileged_syscalls_blocked():
    profile = json.loads((Path(__file__).parents[1] / "sandbox/chromium-seccomp-oci.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert "SCMP_ARCH_X86_64" in profile["architectures"]
    allowed = set()
    for rule in profile["syscalls"]:
        assert set(rule) <= {"names", "action", "args", "errnoRet"}
        if rule["action"] == "SCMP_ACT_ALLOW":
            allowed.update(rule["names"])
    assert {"clone", "unshare", "setns", "chroot"} <= allowed
    assert not {"mount", "bpf", "reboot", "delete_module", "clock_settime"} & allowed
