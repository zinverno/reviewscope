"""Materialize an inventoried private backup into newly allocated storage before replay."""

import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid4

from operational.delivery_objects import encoded
from reviewscope.access.ledger import digest, ledger_path, read_witness
from reviewscope.access.restore import (
    NONCE,
    READ,
    child_dir,
    directory,
    identity,
    mount_id,
    private,
    private_bytes,
    receipt,
    require,
    sha,
    uuid_string,
    volume_authority,
)

MAX_FILES = 20_000
MAX_BYTES = 4 * 1024**3
MAX_FILE = 256 * 1024**2
WRITE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def names(fd, limit):
    items = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            require(len(items) < limit, "inventory_limit")
            items.add(entry.name)
    return items


def inventory(path, expected):
    raw = private_bytes(path)
    require(sha(expected) and hashlib.sha256(raw).hexdigest() == expected, "inventory_unverified")

    def distinct(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), "invalid_inventory")
        return result

    value = json.loads(raw, object_pairs_hook=distinct)
    require(
        isinstance(value, dict)
        and set(value) == {"version", "backup_id", "workspaces"}
        and value["version"] == 1
        and uuid_string(value["backup_id"]),
        "invalid_inventory",
    )
    spaces = value["workspaces"]
    require(isinstance(spaces, dict) and 0 < len(spaces) <= 256, "invalid_inventory")
    count = size = 0
    for w, files in spaces.items():
        require(uuid_string(w) and isinstance(files, dict), "invalid_inventory")
        for key, n in files.items():
            require(sha(key) and type(n) is int and 0 <= n <= MAX_FILE, "invalid_inventory")
            count += 1
            size += n
    require(count <= MAX_FILES and size <= MAX_BYTES, "inventory_limit")
    return spaces


def transfer(src, dst, key, size):
    with ExitStack() as stack:
        f = stack.enter_context(os.fdopen(os.open(key, READ, dir_fd=src), "rb"))
        st = private(f.fileno())
        require(st.st_size == size and mount_id(f.fileno()) == mount_id(src), "inventory_mismatch")
        out = None
        if dst is not None:
            out = stack.enter_context(os.fdopen(os.open(key, WRITE, 0o400, dir_fd=dst), "wb"))
            post = private(out.fileno())
            require(not os.path.samestat(st, post) and mount_id(out.fileno()) == mount_id(dst))
        checksum = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = f.read(min(64 * 1024, remaining))
            require(chunk, "inventory_mismatch")
            remaining -= len(chunk)
            checksum.update(chunk)
            if out is not None:
                out.write(chunk)
        require(not f.read(1) and checksum.hexdigest() == key, "inventory_mismatch")
        if out is not None:
            out.flush()
            os.fsync(out.fileno())
        return [key, size, identity(out.fileno() if out is not None else f.fileno())]


def walk(src, spaces, dst=None):
    present = names(src, 256)
    require(present <= spaces.keys(), "inventory_mismatch")
    tree = []
    dirs = {}
    for w, files in sorted(spaces.items()):
        with ExitStack() as stack:
            out = None
            if dst is not None:
                dw = stack.enter_context(child_dir(dst, w, create=True))
                out = stack.enter_context(child_dir(dw, "sha256", create=True))
                dirs[w] = [identity(dw), identity(out)]
            if w not in present:
                require(not files and dst is not None, "inventory_mismatch")
                continue
            ws = stack.enter_context(child_dir(src, w))
            require(names(ws, 1) == {"sha256"}, "inventory_mismatch")
            objects = stack.enter_context(child_dir(ws, "sha256"))
            require(names(objects, MAX_FILES) == files.keys(), "inventory_mismatch")
            if dst is None:
                dirs[w] = [identity(ws), identity(objects)]
            for key, size in sorted(files.items()):
                tree.append([w, *transfer(objects, out, key, size)])
            if out is not None:
                os.fsync(out)
    return digest(tree), dirs


def exclusive_record(path, data):
    # Only the operator holds the exclusive authority lock. Publish a complete record.
    with directory(path.parent, storage=False) as parent:
        try:
            existing = private_bytes(path)
        except FileNotFoundError:
            pass
        else:
            require(existing == data, "authority_invalid")
            return
        temp = ".pending-" + str(uuid4())
        with os.fdopen(os.open(temp, WRITE, 0o400, dir_fd=parent), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(temp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)


def prepare(conn, source, destination, manifest, inventory_sha, expected, access, erased=()):
    from operational.retention import exclusive, stamp, verify_access_permissions

    with exclusive(conn):
        witness = read_witness()
        _, target = stamp(conn)
        require(
            sha(access)
            and witness["sha256"] == expected
            and target["installation"] == witness["body"]["binding"]["installation"]
            and target != witness["body"]["binding"],
            "restore_target_unverified",
        )
        verify_access_permissions(conn, access)
        authority = ledger_path().parent
        for other in (source, destination, manifest):
            require(
                not authority.is_relative_to(other) and not other.is_relative_to(authority),
                "independent_authority_required",
            )
        require(
            not source.is_relative_to(destination)
            and not destination.is_relative_to(source)
            and not manifest.is_relative_to(source)
            and not manifest.is_relative_to(destination),
            "independent_objects_required",
        )
        spaces = inventory(manifest, inventory_sha)
        ledger_only = sorted(str(w) for w in erased)
        confirmed = {event[1] for event in witness["body"]["events"] if event[2] == "workspace"}
        require(
            len(spaces) + len(ledger_only) <= 256
            and len(set(ledger_only)) == len(ledger_only)
            and set(ledger_only) <= confirmed - set(spaces),
            "invalid_erased_workspace",
        )
        with directory(source) as src, directory(destination.parent) as parent:
            require(not names(src, 256).intersection(ledger_only), "inventory_mismatch")
            spaces.update({w: {} for w in ledger_only})
            require(destination.name not in {"", ".", ".."}, "invalid_store")
            # A mistaken child of a relocated live archive is outside the allocated parent.
            parent_id = identity(parent)
            require(parent_id == volume_authority()["destination_parent"], "destination_scope")
            with child_dir(parent, destination.name, create=True) as dst:
                nonce = str(uuid4())
                os.setxattr(dst, NONCE, nonce.encode(), os.XATTR_CREATE)
                tree, dirs = walk(src, spaces, dst)
                require(walk(dst, spaces) == (tree, dirs), "copy_changed")
                os.fsync(dst)
                doc = {
                    "version": 2,
                    "volume_authority": os.environ["RS_RESTORE_VOLUME_SHA256"],
                    "nonce": nonce,
                    "root": str(destination),
                    "identity": identity(dst),
                    "inventory": inventory_sha,
                    "ledger_only": ledger_only,
                    "tree": tree,
                    "spaces": dirs,
                    "target": target,
                    "ledger": expected,
                    "access": access,
                }
                # Save the exact approved inventory privately for independent re-verification.
                raw = private_bytes(manifest)
                require(hashlib.sha256(raw).hexdigest() == inventory_sha, "inventory_unverified")
                inv = authority / ("inventory-" + inventory_sha)
                exclusive_record(inv, raw)
                content = encoded(doc)
                key = hashlib.sha256(content).hexdigest()
                exclusive_record(authority / ("restore-" + key), content)
                receipt(authority, key)
                return key


def activate_roots(keys, target, witness, access, *, resume_seal=False):
    require(isinstance(keys, (list, tuple)) and 0 < len(keys) <= 256, "restore_receipt_required")
    require(
        all(sha(key) for key in keys) and len(set(keys)) == len(keys), "restore_receipt_invalid"
    )
    roots = {}
    authority = ledger_path().parent
    for key in keys:
        doc = receipt(authority, key)
        require(
            doc["target"] == target and (resume_seal or doc["ledger"] == witness["sha256"]),
            "restore_receipt_stale",
        )
        require(doc["access"] == access, "access_review_required")
        spaces = inventory(authority / ("inventory-" + doc["inventory"]), doc["inventory"])
        spaces.update({w: {} for w in doc["ledger_only"]})
        root = Path(doc["root"])
        with directory(root) as fd:
            require(walk(fd, spaces) == (doc["tree"], doc["spaces"]), "prepared_copy_changed")
        for w in spaces:
            require(UUID(w) not in roots, "overlapping_receipts")
            roots[UUID(w)] = root
    return roots
