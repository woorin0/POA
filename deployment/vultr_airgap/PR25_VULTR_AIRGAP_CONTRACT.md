# PR25 → PR26: Vultr Ubuntu air-gap & permission contract (NO deployment)

**Result is an isolated, unstartable systemd policy candidate and GitHub CI; not a Vultr production installation.**

Parent PR25 final PASS HEAD: `db343f2cda151fd396f58b5d8d0544bb7e5c5e6f` (Git Tree `2eade4630335344248b4fb159c0d411a98fe84c1`). Parent PR25 Windows/Ubuntu audit run: https://github.com/woorin0/POA/actions/runs/42432091126, 161 + 52 each OS = 213 each OS, Mock PASS. PR24 original `df0d7faad850c2435ec77c5790dd7bf0ed2f1e3c`. Main baseline `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`. **All product, financial calculations, order entrypoints, Safe Retry, PnL/ledger, DB schemas, original 72-file manifest, services, package files and operating secrets are unchanged.** Only new `deployment/vultr_airgap/`, `tests/test_pr25_vultr_airgap.py`, this document and new CI workflow are added.

## 1. Inert unit — contract, not installation

`deployment/vultr_airgap/poa-airgap.service.candidate` targets a **separate, new Ubuntu 22.04 amd64 test host**. It is NOT an installable service in this PR: no `[Install]` target, `RefuseManualStart=yes`, `ExecStartPre=/usr/bin/false`, `ExecStart=/usr/bin/false`, `Restart=no`, no `EnvironmentFile` or production executable. Never copy or enable on operating BOT03, never reinstall an existing server, and never attach real DB/API keys.

Proposed future systemd isolation uses overlapping controls, all of which must be proven on a separate host **before** any operational authorization:
- Network: `PrivateNetwork=yes` (independent namespace); `RestrictAddressFamilies=none` (disallow socket(2) families); `IPAddressDeny=any` (cgroup egress policy where supported); `SystemCallFilter=~@network-io @privileged @mount io_uring_setup io_uring_enter io_uring_register`, `SystemCallArchitectures=native`, `SystemCallErrorNumber=EPERM`.
- Privilege: `DynamicUser=yes`, `NoNewPrivileges=yes`, `CapabilityBoundingSet=`, `AmbientCapabilities=`, `RestrictNamespaces=yes`, `RestrictSUIDSGID=yes`, `PrivateDevices=yes`, `ProtectHome=yes`, `ProtectSystem=strict`, `ProtectControlGroups=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes` plus resource/process isolation. It offers no path to read legacy `/root/db/pb_data`, secret env or existing .env.
- Anti-bypass: do not allow socket unit activation, `ListenStream`, `FileDescriptorStore`, `JoinsNamespaceOf`, `NetworkNamespacePath`, `BindPaths`, `+!` exec prefixes, arbitrary `ExecStartPre` scripts, or already-open sockets. At rollout, independently enumerate all systemd socket-activation units, passed FDs, executable paths, inherited environment, root/admin permission, Tailscale/SSH proxies and outbound network/loopback, including siblings and nested subprocesses. **None of this host validation has happened.**

**Important:** The prototype is intentionally so restrictive that a future HTTP/Uvicorn listener cannot work in it. Do not silently loosen socket limits to make a webhook service run. The only permitted future design is an independently audited non-trading IPC gateway with explicit request proof and no order capability. This branch does not implement that future gateway.

## 2. Linux kernel-level proof and limitations

`deployment/vultr_airgap/seccomp_rehearsal.py` runs as a **separate disposable CI child process** only. It calls `prctl(PR_SET_NO_NEW_PRIVS)`, uses the system `libseccomp` library to deny socket/socketpair/connect/bind/listen, network send/recv, io_uring, namespace changes and pidfd_getfd, and checks `socket(AF_INET)`, `socket(AF_INET6)`, direct C `libc.socket`, and a newly exec'd Python subprocess inheriting seccomp. **It never calls connect() or sends packets.** The CCXT and KIS paths are **synthetic transport probes**, not real trading SDK calls. A missing library, rejected filter rule or unexpected successful socket creation fails the job.

Ubuntu 22.04 GitHub Actions tests actual Linux kernel syscall denial and `systemd-analyze verify` on a copied candidate unit file, but NEVER starts it. The Ubuntu runner is not the actual Vultr server or its PID 1 systemd; hence **the CI does not establish actual Vultr host isolation**.

Windows checks identical policy static assertions, input/override mock tests and synthetic socket denial but **does NOT run seccomp or prove a Linux syscall policy**.

## 3. Known bypasses and required external acceptance gates

- `VULTR_HOST_VERIFIED=NO`: no Vultr login, no actual host, no firewall/cgroup/namespace verification, no order/API account access, no installations or deployment.
- `INHERITED_SOCKET_FD=UNVERIFIED`: systemd `RestrictAddressFamilies` cannot revoke sockets inherited from socket activation or another process. Child processes may inherit exposed file descriptors unless the supervisor explicitly closes them; the Linux rehearsal runs child `subprocess` with `close_fds=True`. At Vultr, independent kernel/FD inspection is required.
- `ACTUAL_SYSTEMD_EXECUTION=NOT_PERFORMED`: config parsing, `systemd-analyze verify` and standalone seccomp are *not* equivalent to executing under the target systemd unit. Strict opt-out prevents starting this contract accidentally.
- `RAW_CCXT=SIMULATED_TRANSPORT_ONLY`: a fake CCXT-like invocation attempts socket creation; no imported real CCXT trading client, signed real HTTP request or account credential.
- `WINDOWS_SECCOMP=MOCK_ONLY`: actual OS seccomp only on Ubuntu/Linux CI.
- Arbitrary Python code, privileged root access, separately running old POA process, leaked exchange API keys, inherited TCP sockets, malicious host proxy or privileged `CAP_SYS_ADMIN` are not proven contained. A process with only an in-process Python guard is inadequate; host policy is a separate hard requirement.
- No real closed-loop order request even to a test exchange; no reproduction of 203 concurrent signals on a host; no DB backup/restore; no read-only key same-account verification; no actual S3/Object Lock provider approval.
- `LIVE_ORDERS=NOT_EXECUTED`, `OPERATING_DB_WRITES=NOT_PERFORMED`, `AWS_REQUIRED=NO`, `OPERATING_READY=BLOCKED`.

## 4. Windows + Ubuntu CI

New `.github/workflows/pr25_vultr_airgap_ci.yml` runs the exact SHA-based additive-file diff on PR26 and the new policy/Mock suite. It independently pins the original PR25 HEAD to run original PR25 161 Mock tests in a detached worktree, plus the PR23 exact 52 stage/read-only Mock tests and 72-file release/packaging gates from detached PR23. No original release pin is weakened or copied into modified product code. New Ubuntu kernel tests are a **real kernel syscall denial exercise** in disposable child processes; Windows results are **Mock/static** only.

`OPERATING_READY=BLOCKED` until Vultr Ubuntu systemd cgroups/namespace, host firewall, external SDK/process egress, file descriptors and privileges are independently tested and separately approved. A future real trading release must also separately prove operational DB restore, read-key rights, original full financial regression and explicit operator authority.

## Official reference

- systemd.exec: https://www.freedesktop.org/software/systemd/man/latest/systemd.exec
- Ubuntu systemd.exec: https://manpages.ubuntu.com/manpages/jammy/man5/systemd.exec.5.html
- Linux seccomp and libseccomp: https://github.com/seccomp/libseccomp
