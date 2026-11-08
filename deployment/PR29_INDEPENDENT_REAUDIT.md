# Independent PR #29 security re-audit — synthetic only; no operational release

**Exact audited parent:** `89797bd6214011e7b0e3a6f9ab1e96b845773f45`, PR #29 Actions `42438412003` Windows/Ubuntu 2/2 success. This is an isolated stacked **Draft** audit/fix PR. Production main remains `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`; original financial product is protected.

## Findings and minimal remediation

1. **Module-cache poisoning after role seal:** the PR #29 import finder checks import-time module names and already-loaded modules only at startup. An adversary controlling Python code can later inject a fake `sys.modules["ccxt"]`, `main`, `exchange.safe_retry` or `exchange.stock.kis`, and `importlib.import_module` returns the cached object; a fake order function can run before the next request. **Fix:** add `assert_seal_intact(role)` at each new sealed Stage/Read-only ASGI request, and at repeat `seal_role`. This fails the next request on forbidden cache names or removed/reordered import finder. It is a **detection at request boundary only**, not an application-wide prevention of compromised in-process code.
2. **Inherited Linux socket FD:** Python audit restrictions on new socket creation do not revoke a socket inherited before sealing. **Fix:** on Linux, fail closed before sealing if `/proc/self/fd` is unreadable or shows any `socket:[...]` entry. Tests pass only synthetic unconnected AF_INET and AF_UNIX socketpair descriptors from parent to child; no connect, HTTP or orders. This preflight cannot retroactively revoke sockets created by privileged/native code after sealing. Windows inherited HANDLEs are not attested.
3. **Import/audit tamper:** removing `sys.meta_path` finder post-seal is detected at next request. CPython audit events deny the tested socket/subprocess/shell operations. Native interpreter compromise, replacing Python bytecode ahead of seal, malicious `sys.modules["poa_runtime.sealed_boundary"]` before first import or forged code objects remain in scope of an external trust anchor, **not Python-level proofs**.
4. **Separate-process bypass:** `main.py`, raw CCXT/KIS calls, stock order and Safe Retry workers started independently are not constrained by the Stage/Read-only Python hook. External process isolation must use OS/process/network policy; this PR does not pretend the original trading engine has a global kill switch.
5. **Inherited non-socket pipes/IPC:** `os.write` on a preopened pipe is not blocked by these CPython audit events, nor does the Linux socket scan detect unnamed pipes. This remains a negative control. Root/host namespaces, systemd socket activation, FDs, global firewall and service unit enforcement all need independent host verification.

## Verification

- Run independent disposable interpreter negative/positive Mock E2E on Windows and Ubuntu; exact parent source regression check plus child Linux inherited FD test (Windows marked unavailable, **not PASS**).
- Inherit existing PR #29 Stage/Read-only Mock and 203-way synthetic concurrency, exact immutable main/finance/DB/72 Manifest/packaging/source-pin tests; update only two exact-scope allowlists to include this audit's three added files, never bypass exclusions.
- New Actions workflow runs independent release checks, Original HTTP/v1, Ubuntu 4-venue native fill confirmation, Golden and Safe Retry, and full Windows pytest on the **same final audit candidate HEAD**.
- All attacks are synthetic. No actual main/exchange import, live SDK invocation, network connect, API keys, production DB, service activation or live order.

## Explicit residual labels

`CACHE_INJECTION_BEFORE_NEXT_REQUEST=UNCONTROLLED`
`BEFORE_SEAL_MODULE_SPOOF=UNCONTROLLED`
`ARBITRARY_EXEC_CODE=UNCONTROLLED`
`INHERITED_NON_SOCKET_IPC=UNCONTROLLED`
`WINDOWS_INHERITED_HANDLES=UNVERIFIED`
`LEGACY_MAIN_OTHER_PROCESS=UNCONTROLLED`
`RAW_CCXT_KIS_OTHER_PROCESS=UNCONTROLLED`
`VULTR_OS_POLICY=UNVERIFIED`
`FINANCIAL_SOURCE=UNCHANGED`
`ACTUAL_ORDERS=0`
`PRODUCTION_READY=BLOCKED`

**Do not merge, deploy, enable systemd services, inject real keys, restore or write operating DB or send any real orders.** No production/global order safety claim is allowed on the basis of this audit.
