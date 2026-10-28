import os
import pathlib
import sys
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]

_GUARD_INSTALLED = False


def _audit_guard(event, args):
    if event == "socket.connect":
        frame = sys._getframe(1)
        for _ in range(8):
            if frame is None:
                break
            if frame.f_code.co_name == "socketpair" and pathlib.Path(frame.f_code.co_filename) == pathlib.Path(
                __import__("socket").__file__
            ):
                return
            frame = frame.f_back
    if event in ("socket.connect", "socket.connect_ex", "socket.sendto", "socket.getaddrinfo"):
        raise RuntimeError("AUDIT_NETWORK_DISABLED")
    if event == "sqlite3.connect":
        name = os.fsdecode(args[0])
        if name != ":memory:":
            if name.startswith("file:"):
                name = unquote(urlsplit(name).path)
                if len(name) > 2 and name[0] == "/" and name[2] == ":":
                    name = name[1:]
            p = pathlib.Path(name).resolve()
            temp_dir = pathlib.Path(os.environ.get("TEMP", "/tmp")).resolve()
            allowed = (
                (pathlib.Path("C:/Users/Public/Documents/ESTsoft/CreatorTemp") in p.parents)
                or (ROOT in p.parents)
                or (temp_dir in p.parents)
            )
            if not allowed or p.name in ("data.db", "store.db", "logs.db"):
                raise RuntimeError(
                    "AUDIT_PRODUCTION_DB_DISABLED: " + str(p) + " root=" + str(ROOT) + " temp=" + str(temp_dir)
                )


if not _GUARD_INSTALLED:
    sys.addaudithook(_audit_guard)
    _GUARD_INSTALLED = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
