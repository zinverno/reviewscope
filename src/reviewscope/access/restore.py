"""Local Linux restore receipts. Only the offline operator writes the authority directory."""

import ctypes
import hashlib
import json
import os
import re
import stat
import struct
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
LIMIT = 4 * 1024 * 1024
NONCE = "user.reviewscope.restore"


class RestoreError(ValueError):
    """Stable diagnostic without path or operating-system exception text."""


def require(ok, code="restore_receipt_invalid"):
    if not ok:
        raise RestoreError(code)


def sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def uuid_string(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def mount_id(fd):
    fields = Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
    ids = [int(row.split()[1]) for row in fields if row.startswith("mnt_id:")]
    require(len(ids) == 1, "mount_unverified")
    return ids[0]


def identity(fd):
    st = os.fstat(fd)
    return [st.st_dev, st.st_ino, unique_mount(fd)]


def unique_mount(fd):
    # Linux statx UAPI: stx_mask at 0, stx_mnt_id at 0x90. The unique ID lasts until reboot.
    statx = getattr(ctypes.CDLL(None), "statx", None)
    output = ctypes.create_string_buffer(256)
    mask = 0x4000  # STATX_MNT_ID_UNIQUE, Linux 6.8+
    require(
        statx is not None
        and statx(fd, b"", 0x1000, mask, ctypes.byref(output)) == 0
        and struct.unpack_from("=I", output)[0] & mask,
        "mount_authority_unavailable",
    )
    return struct.unpack_from("=Q", output, 0x90)[0]


def volume_authority():
    # The independent storage custodian pins this record at native mount creation.
    try:
        expected = os.environ["RS_RESTORE_VOLUME_SHA256"]
        raw = private_bytes(Path(os.environ["RS_RESTORE_VOLUME_RECORD"]), storage=False)
        require(sha(expected) and hashlib.sha256(raw).hexdigest() == expected)
        record = json.loads(raw)
        require(
            record["version"] == 1
            and record["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            and isinstance(record["native_mounts"], list)
            and 0 < len(record["native_mounts"]) <= 64
            and all(type(i) is int and i > 0 for i in record["native_mounts"])
            and isinstance(record["destination_parent"], list)
            and len(record["destination_parent"]) == 3
        )
        return record
    except (KeyError, OSError, TypeError, ValueError, RecursionError):
        raise RestoreError("mount_authority_unavailable") from None


def check_mount(fd):
    """Conservative local profile. Refuse submount aliases and multiply mounted devices."""
    require(unique_mount(fd) in volume_authority()["native_mounts"], "mount_unverified")
    mid = str(mount_id(fd))
    mounts = [row.split() for row in Path("/proc/self/mountinfo").read_text().splitlines()]
    records = [r for r in mounts if r[0] == mid]
    require(len(records) == 1, "mount_unverified")
    entry = records[0]
    require(
        entry[3] == "/"
        and entry[entry.index("-") + 1] in {"tmpfs", "ext4", "xfs", "btrfs"}
        and sum(r[2] == entry[2] for r in mounts) == 1,
        "mount_unverified",
    )


def private(fd, *, directory=False):
    st = os.fstat(fd)
    require(st.st_uid == os.getuid() and not st.st_mode & 0o077, "private_store_required")
    if directory:
        require(stat.S_ISDIR(st.st_mode), "private_store_required")
    else:
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "unexpected_object")
    return st


@contextmanager
def directory(path, *, storage=True):
    require(path.is_absolute() and path != Path("/") and ".." not in path.parts, "invalid_store")
    fd = os.open("/", DIR)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = child
            try:
                os.stat("PG_VERSION", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RestoreError("independent_authority_required")
            st = os.fstat(fd)
            require(
                st.st_uid in {0, os.getuid()}
                and (not st.st_mode & 0o022 or st.st_uid == 0 and st.st_mode & stat.S_ISVTX),
                "untrusted_parent",
            )
        private(fd, directory=True)
        if storage:
            check_mount(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def child_dir(parent, name, *, create=False):
    if create:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    fd = os.open(name, DIR, dir_fd=parent)
    try:
        private(fd, directory=True)
        require(mount_id(fd) == mount_id(parent), "mount_unverified")
        yield fd
    finally:
        os.close(fd)


def private_bytes(path, *, storage=False):
    with (
        directory(path.parent, storage=storage) as parent,
        os.fdopen(os.open(path.name, READ, dir_fd=parent), "rb") as f,
    ):
        st = private(f.fileno())
        require(
            mount_id(f.fileno()) == mount_id(parent) and st.st_size <= LIMIT, "authority_invalid"
        )
        data = f.read(LIMIT + 1)
        require(len(data) == st.st_size, "authority_invalid")
        return data


def receipt(authority, key):
    require(sha(key))
    try:
        raw = private_bytes(authority / ("restore-" + key))
        require(hashlib.sha256(raw).hexdigest() == key)
        doc = json.loads(raw)
        require(
            doc["version"] == 2
            and sha(doc["volume_authority"])
            and doc["volume_authority"] == os.environ.get("RS_RESTORE_VOLUME_SHA256")
            and uuid_string(doc["nonce"])
            and sha(doc["inventory"])
            and sha(doc["tree"])
            and sha(doc["ledger"])
            and sha(doc["access"])
            and isinstance(doc["ledger_only"], list)
            and all(uuid_string(w) and w in doc["spaces"] for w in doc["ledger_only"])
            and isinstance(doc["spaces"], dict)
            and 0 < len(doc["spaces"]) <= 256
        )
        require(all(uuid_string(w) for w in doc["spaces"]))
        raw_inventory = private_bytes(authority / ("inventory-" + doc["inventory"]))
        require(hashlib.sha256(raw_inventory).hexdigest() == doc["inventory"])
        with directory(Path(doc["root"])) as root:
            require(
                identity(root) == doc["identity"]
                and os.getxattr(root, NONCE) == doc["nonce"].encode()
            )
            for w, ids in doc["spaces"].items():
                with child_dir(root, w) as ws, child_dir(ws, "sha256") as objects:
                    require([identity(ws), identity(objects)] == ids)
        return doc
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        raise RestoreError("restore_receipt_invalid") from None
