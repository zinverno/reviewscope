"""A bounded local message between the TLS endpoint and its separately launched importer."""

import json
import os
import socket
import stat
from pathlib import Path
from uuid import UUID

from reviewscope.import_preview.contract import MAX_BYTES, MAX_CONFIG


class UploadError(ValueError):
    pass


def sizes(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise UploadError("invalid_upload")
    for n, cap in zip(values, (MAX_CONFIG, MAX_CONFIG, MAX_BYTES), strict=True):
        if type(n) is not int or n < 1:
            raise UploadError("invalid_upload")
        if n > cap:
            raise UploadError("over_limit")
    return tuple(values)


def socket_path():
    path = Path(os.environ.get("RS_UPLOAD_SOCKET", ""))
    if not path.is_absolute() or path.parent != path.parent.resolve(strict=True):
        raise UploadError("importer_unavailable")
    st = path.parent.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise UploadError("importer_unavailable")
    return path


def read_json(stream, limit=4096):
    prefix = stream.read(4)
    if len(prefix) != 4:
        raise UploadError("invalid_upload")
    length = int.from_bytes(prefix)
    if not 0 < length <= limit:
        raise UploadError("invalid_upload")
    value = stream.read(length)
    if len(value) != length:
        raise UploadError("invalid_upload")
    return json.loads(value)


def write_json(stream, value, limit=4096):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > limit:
        raise UploadError("invalid_upload")
    stream.write(len(raw).to_bytes(4))
    stream.write(raw)


def exchange(message, parts, reply_limit=4096):
    path = socket_path()
    st = path.lstat()
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise UploadError("importer_unavailable")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(120)
        sock.connect(str(path))
        with sock.makefile("rwb") as stream:
            write_json(stream, message)
            for part in parts:
                stream.write(part)
            stream.flush()
            sock.shutdown(socket.SHUT_WR)
            return read_json(stream, reply_limit)


def commit(message, parts):
    result = exchange(message, parts)
    if not isinstance(result, dict) or "snapshot_id" not in result:
        reason = result.get("error") if isinstance(result, dict) else None
        raise UploadError("scope_denied" if reason == "scope_denied" else "importer_unavailable")
    return str(UUID(result["snapshot_id"]))
