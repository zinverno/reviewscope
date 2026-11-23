"""Workspace-scoped, write-once files for the local operator."""

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


class DeliveryError(ValueError):
    """Arguments are stable codes intended for the operator."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def private_directory(directory):
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or directory != directory.resolve(strict=True)
    ):
        raise DeliveryError("private_store_required")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def object_directory(root: Path, workspace, *, create):
    private_directory(root)
    directory = root
    for part in (str(workspace), "sha256"):
        child = directory / part
        if create:
            child.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory)
        private_directory(child)
        directory = child
    return directory


@contextmanager
def open_object(directory, digest):
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
        raise DeliveryError("invalid_digest")
    path = directory / digest
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise DeliveryError("object_verification_failed")

        yield stream


def verify_file(directory, digest, expected_size=None):
    with open_object(directory, digest) as stream:
        if expected_size is not None and os.fstat(stream.fileno()).st_size != expected_size:
            raise DeliveryError("object_verification_failed")
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise DeliveryError("object_verification_failed")


def publish(directory, chunks, expected=None):
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=directory) as staged:
        for chunk in chunks:
            staged.write(chunk)
            digest.update(chunk)
        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            raise DeliveryError("checksum_mismatch")
        staged.flush()
        os.fchmod(staged.fileno(), 0o400)
        os.fsync(staged.fileno())
        staged.seek(0)
        if hashlib.file_digest(staged, "sha256").hexdigest() != actual:
            raise DeliveryError("checksum_mismatch")
        try:
            os.link(staged.name, directory / actual)
        except FileExistsError:
            pass
        verify_file(directory, actual, os.fstat(staged.fileno()).st_size)
        sync_directory(directory)
    return actual
