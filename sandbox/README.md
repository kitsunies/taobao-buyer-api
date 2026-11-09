# Linux browser sandbox profiles

Chromium runs with its sandbox enabled. The shipped container starts as a
nonroot user, drops every capability, uses a read-only root filesystem with
`no-new-privileges`, and refuses to start if the offline sandbox check fails.
The files here are the generic profiles needed by Docker, containerd or
Kubernetes when the host restricts user namespaces. They contain no deployment
or cluster-specific settings.

- `chromium-apparmor` derives from Moby v26.1.5's
  `profiles/apparmor/template.go`, adding only `userns` permission to the
  default container rules. Load it with
  `apparmor_parser -r sandbox/chromium-apparmor`, then select the profile as
  `taobao-buyer-chromium`. Both files are Apache-2.0 licensed; see
  `PLAYWRIGHT_LICENSE`.
- `chromium-seccomp.json` derives from the Playwright v1.64.0 profile at
  `https://github.com/microsoft/playwright/blob/v1.64.0/utils/docker/seccomp_profile.json`.
  It adds `chroot` to the allowed syscalls because Chromium chroots inside its
  own user namespace. The outer container retains no capabilities, including
  `SYS_CHROOT`. Docker resolves this profile's capability and architecture
  conditions before passing it to the runtime; use it with Docker.
- `chromium-seccomp-oci.json` is the same policy resolved for Linux amd64,
  kernel 4.8 or later, with all capabilities dropped. Kubernetes does not
  resolve Docker's `archMap`, `includes` or `excludes` fields, so give its
  `seccompProfile.localhostProfile` a node path where this file is installed
  with root ownership and mode 0644. Do not point a cluster at the
  Docker-dialect file.

No host sysctl changes and no extra container capabilities are required. Never
add `--no-sandbox` to work around a failed sandbox check. The full Chromium
channel is used because Chromium's headless shell lacks `chrome://sandbox`.

The CI workflow loads both dialects under `--network none` and runs the offline
browser checks with them. `tests/test_deployment.py` guards the resolved OCI
policy.
