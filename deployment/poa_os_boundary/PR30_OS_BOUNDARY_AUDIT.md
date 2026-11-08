# POA PR #30 PASS HEAD — independent Linux OS-level order-egress audit (Draft)

- Fixed parent: `554ccab03c05e2e4d4a50f75630500704b4654e3` (PR #30 Windows/Ubuntu 2/2 PASS, Actions #42441014961).
- Base main: `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`; unchanged. Original order engine, Safe Retry, finance, ledger, database, package manifests (72), existing systemd units, installers and all API keys remain untouched.
- Reuse: the independent **Linux libseccomp methodology** already demonstrated in PR #27. New branch carries a self-contained copy narrowed to this audit; it does not merge the prior research PR or modify its files.

## Threat model / scope

An attacker starts **a separate process**, bypassing the Stage/Read-only Python `sys.meta_path` and audit hooks, and attempts to run original `main.py`, raw CCXT, raw KIS, or a Safe Retry restart. A distinct process is entirely outside PR #30's CPython isolation unless its **OS-level process tree** is explicitly contained. This Draft adds **no** global POA order interceptor; it verifies a separate, kernel-enforced network syscall barrier in a disposable child process and all its descendants.

Kernel probe executes synthetic transport stubs labeled `main`, `ccxt`, `kis`, `safe-retry`. It checks new `socket(AF_INET/AF_INET6)` calls are denied and that **after exec** child interpreters inherit seccomp without reinstalling it. A direct **libc.socket** syscall via FFI is also denied. The probe never loads/executes the real order engine or real SDKs, opens exchange accounts, connects to external addresses, or submits orders. It checks `no_new_privs`, required syscall resolution and filter-install success and fails closed if any fails. Windows tests code/policy contracts and mocked syscall denial only; there is no Windows kernel seccomp proof.

## Inherited FD bypass and containment

`RestrictAddressFamilies` and seccomp filters on `socket()` **do not revoke existing connected socket FDs**; `os.write` on such a preopened descriptor may send data, and a compromised sibling could act as an order proxy. The child preflight fails closed if Linux `/proc/self/fd` lists a socket FD (including a synthetic AF_UNIX socketpair deliberately passed by the test parent). All nested execs use `close_fds=True` and a sanitized synthetic environment. Actual systemd socket activation, passed descriptors, inherited pipes/IPC, Unix domain proxy access, Tailscale, Docker socket, host root or a separate unconfined service remain unverified.

## OS service candidate is intentionally unstartable

The only added systemd service is **not installed, enabled or started**. It has no `[Install]`, `RefuseManualStart=yes`, `ExecStartPre=/usr/bin/false`, `ExecStart=/usr/bin/false`, no env files or real process, and no secret/key mounts. The static unit contract applies `PrivateNetwork=yes`, `RestrictAddressFamilies=none`, `IPAddressDeny=any`, `SystemCallFilter=~@network-io ...`, `NoNewPrivileges`, unprivileged DynamicUser and host filesystem restrictions. Ubuntu CI runs `systemd-analyze verify` on a temporary renamed copy but **never systemctl start**. This policy blocks ordinary HTTP listeners too: it cannot be used as an actual working web service without a separately reviewed non-trading IPC gateway.

## Explicit outcome labels

`LINUX_CI_KERNEL_SECCOMP=EXECUTED` only if Ubuntu job passes.
`WINDOWS_KERNEL_SECCOMP=NOT_SUPPORTED`
`SEPARATE_PROCESS_SYNTHETIC_MAIN_CCXT_KIS_RETRY=MOCK_ONLY`
`REAL_MAIN_ORDER_EXECUTION=NOT_PERFORMED`
`VULTR_HOST_SYSTEMD=UNVERIFIED`
`HOST_WIDE_ORDER_DENY=UNVERIFIED`
`EXTERNAL_UNCONFINED_PROCESS=UNCONTROLLED`
`INHERITED_PIPE_IPC_PROXY=UNCONTROLLED`
`ACTUAL_ORDERS=0`
`FINANCIAL_SOURCE=UNCHANGED`
`PRODUCTION_READY=BLOCKED`

**NO MERGE, DEPLOY, REAL KEYS, OPERATING DB ACCESS, SYSTEMD ACTIVATION OR LIVE ORDERS.**
