"""Intentionally inert sandbox payload — NEVER launch for live POA.

Invoked only if a privileged operator has already removed the immutable
ExecStartPre=/usr/bin/false hard-stop from an UNAPPROVED candidate. Even then
this process will not import CCXT, KIS, POA, HTTP, sockets, DB or subprocess.
"""
from __future__ import annotations


def main() -> int:
    return 77  # Always non-zero, no service or network activation.


if __name__ == "__main__":
    raise SystemExit(main())
