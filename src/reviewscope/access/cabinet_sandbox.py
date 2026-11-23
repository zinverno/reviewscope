"""Filesystem permissions for the local Linux TLS frontend when browser intake is enabled."""

import ctypes
import os
import platform
import stat
import struct
import sysconfig
import tempfile
from importlib.util import find_spec
from pathlib import Path

from .restore import directory

# Linux Landlock ABI 3 covers truncate and all earlier filesystem mutations.
READ, WRITE, REMOVE, MAKE, TRUNCATE = 4, 2, 32, 256, 16384
HANDLED = ((1 << 15) - 1) & ~(1 << 3)  # The receipt verifier needs directory descriptors.


def confine():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise ValueError("cabinet_profile_unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(444, 0, 0, 1) < 3:
        raise ValueError("cabinet_profile_unavailable")
    parent = Path(os.environ["RS_CABINET_STAGING"])
    with directory(parent, storage=False):
        staging = Path(tempfile.mkdtemp(prefix="intake-", dir=parent))
    authority = Path(os.environ["RS_RETENTION_LEDGER"]).parent
    read = [
        authority,
        Path(os.environ["RS_ACCESS_KEY_FILE"]),
        Path(os.environ["RS_ACCESS_TLS_CERT"]),
        Path(os.environ["RS_ACCESS_TLS_KEY"]),
        Path(sysconfig.get_path("stdlib")),
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parents[1] / "import_preview",
        Path(__file__).resolve().parents[3] / "prototypes/reviewscope-ui/dist",
        Path("/proc/self/mountinfo"),
        Path("/proc/self/fdinfo"),
        Path("/proc/sys/kernel/random/boot_id"),
        Path("/usr/share/zoneinfo"),
    ]
    for name in ("PGPASSFILE", "PGSERVICEFILE", "RS_RESTORE_VOLUME_RECORD"):
        if os.getenv(name):
            read.append(Path(os.environ[name]))
    # Read installed service packages only. The checkout and home directory are excluded.
    for name in (
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic_core",
        "anyio",
        "sniffio",
        "sqlalchemy",
        "psycopg",
        "psycopg_binary",
        "argon2",
        "_argon2_cffi_bindings",
        "cffi",
        "uvicorn",
        "h11",
        "click",
        "greenlet",
        "typing_extensions",
        "annotated_types",
        "annotated_doc",
        "typing_inspection",
        "idna",
    ):
        spec = find_spec(name)
        if spec and spec.origin and spec.origin not in ("built-in", "frozen"):
            path = Path(spec.origin).resolve()
            read.append(path.parent if spec.submodule_search_locations else path)
    for path in ("/etc/passwd", "/etc/localtime", "/dev/urandom"):
        if Path(path).exists():
            read.append(Path(path))
    rules = libc.syscall(444, ctypes.byref(ctypes.c_uint64(HANDLED)), 8, 0)
    if rules < 0:
        raise ValueError("cabinet_profile_unavailable")
    try:
        for path, rights in [(p, READ) for p in read] + [
            (staging, READ | WRITE | REMOVE | MAKE | TRUNCATE)
        ]:
            if not path.is_absolute() or (
                path != staging and (staging.is_relative_to(path) or path.is_relative_to(staging))
            ):
                raise ValueError("cabinet_profile_unavailable")
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if rights != READ and not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise ValueError("cabinet_profile_unavailable")
                rule = ctypes.create_string_buffer(struct.pack("=Qi", rights, fd))
                if libc.syscall(445, rules, 1, ctypes.byref(rule), 0) != 0:
                    raise ValueError("cabinet_profile_unavailable")
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, rules, 0) != 0:
            raise ValueError("cabinet_profile_unavailable")
    finally:
        os.close(rules)
    tempfile.tempdir = str(staging)
