"""Produce an exact Python 3.12-only dependency overlay, without changing POA.

The original requirements.txt, 72-file release manifest, financial code and
legacy Python 3.10/3.11 behavior stay byte-identical. No installation, network,
secret access, OS mutation, DB operation or trading in this utility.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

SOURCE_SHA256 = "e103f650ea4c67a8abcbf7661e8e7e881ba6cef53edf18a8d63a5eaaba1369a4"
LEGACY_LINE = "pendulum==2.1.2"
PY312_LINE = "pendulum==3.2.0"
ROOT = Path(__file__).resolve().parents[1]


class CompatibilityBlocked(ValueError):
    pass


def render_from_bytes(source: bytes) -> bytes:
    """Fail closed if any of the 17 original dependency pins change."""
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise CompatibilityBlocked("PINNED_REQUIREMENTS_SHA256_MISMATCH")
    try:
        raw = source.decode("ascii")
    except UnicodeError:
        raise CompatibilityBlocked("PINNED_REQUIREMENTS_FORMAT_INVALID") from None
    lines = raw.splitlines()
    if len(lines) != 17 or lines.count(LEGACY_LINE) != 1:
        raise CompatibilityBlocked("PINNED_PENDULUM_NOT_UNIQUE")
    if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_\[\],.-]*==[A-Za-z0-9.]+", x) is None
           for x in lines):
        raise CompatibilityBlocked("REQUIREMENTS_FORMAT_UNSAFE")
    changed = [PY312_LINE if line == LEGACY_LINE else line for line in lines]
    if len(changed) != 17 or sum(a != b for a, b in zip(lines, changed)) != 1:
        raise CompatibilityBlocked("NOT_EXACTLY_ONE_PIN_CHANGED")
    return ("\n".join(changed) + "\n").encode("ascii")


def create_overlay(out: Path, requirements: Path = ROOT / "requirements.txt") -> dict:
    """Write only a NEW non-repository file, never replace a pinned artifact."""
    out = Path(out)
    requirements = Path(requirements)
    root = ROOT.resolve()
    if not out.is_absolute():
        raise CompatibilityBlocked("ABSOLUTE_OUTPUT_PATH_REQUIRED")
    target = out.resolve(strict=False)
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise CompatibilityBlocked("DO_NOT_WRITE_IN_REPOSITORY")
    if out.exists() or out.is_symlink():
        raise CompatibilityBlocked("OUTPUT_MUST_NOT_EXIST")
    if not out.parent.is_dir() or out.parent.is_symlink():
        raise CompatibilityBlocked("OUTPUT_PARENT_INVALID")
    if requirements.resolve(strict=False) != (ROOT / "requirements.txt").resolve():
        raise CompatibilityBlocked("UNEXPECTED_SOURCE_PATH")
    if requirements.is_symlink():
        raise CompatibilityBlocked("REQUIREMENTS_SYMLINK_DENIED")
    try:
        content = render_from_bytes(requirements.read_bytes())
    except OSError:
        raise CompatibilityBlocked("REQUIREMENTS_UNREADABLE") from None
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(out, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    except OSError:
        raise CompatibilityBlocked("OUTPUT_CREATION_FAILED") from None
    return {
        "python_target": "3.12",
        "source_pinned_sha256": SOURCE_SHA256,
        "overlay_sha256": hashlib.sha256(content).hexdigest(),
        "exact_pin_override": f"{LEGACY_LINE} => {PY312_LINE}",
        "all_other_pins_unchanged": True,
        "original_requirements_modified": False,
        "install_executed": False,
        "operational_ready": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate Python3.12 dependency overlay only")
    parser.add_argument("--output", type=Path, required=True,
                        help="New absolute path outside POA checkout; no overwrite")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        print("PYTHON312_INTERPRETER_REQUIRED", file=sys.stderr)
        return 75
    try:
        result = create_overlay(args.output)
    except CompatibilityBlocked as exc:
        print(exc, file=sys.stderr)
        return 75
    # No secrets or package logs: only one-line fixed diagnostic.
    print("POA_PY312_DEPENDENCY_OVERLAY_CREATED; TRADING_NOT_ACTIVATED")
    assert result["all_other_pins_unchanged"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
