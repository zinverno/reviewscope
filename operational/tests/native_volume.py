"""Create a fresh native tmpfs for the synthetic backup custodian in its private mount namespace."""

import hashlib
import json
import os
import sys
from pathlib import Path

from reviewscope.access.restore import unique_mount


def main():
    command = sys.argv[1:]
    volume = Path("/mnt")
    area = volume / "generations"
    area.mkdir(mode=0o700)
    control = volume / "custodian"
    control.mkdir(mode=0o700)
    fd = os.open(area, os.O_DIRECTORY)
    try:
        st = os.fstat(fd)
        mount = unique_mount(fd)
        record = {
            "version": 1,
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "native_mounts": [mount],
            "destination_parent": [st.st_dev, st.st_ino, mount],
        }
    finally:
        os.close(fd)
    probe = os.open(volume / "probe-native", os.O_DIRECTORY)
    try:
        record["native_mounts"].append(unique_mount(probe))
    finally:
        os.close(probe)
    raw = json.dumps(record, sort_keys=True).encode()
    authority = control / "native-volume.json"
    authority.write_bytes(raw)
    authority.chmod(0o400)
    env = os.environ | {
        "TMPDIR": str(volume),
        "RS_TEST_GENERATIONS": str(area),
        "RS_RESTORE_VOLUME_RECORD": str(authority),
        "RS_RESTORE_VOLUME_SHA256": hashlib.sha256(raw).hexdigest(),
    }
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
