"""Preview a trusted local delivery. Stdout contains only aggregate evidence."""

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reviewscope.import_preview import preview  # noqa: E402
from reviewscope.import_preview.contract import (  # noqa: E402
    MAX_BYTES,
    MAX_CONFIG,
    MESSAGES,
    PreviewError,
)
from reviewscope.import_preview.reader import read_local  # noqa: E402


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PreviewError("invalid_arguments")


def private_dir(path: Path) -> Path:
    directory = path.absolute()
    root = ROOT / "validation_data" / "private"
    if ".." in directory.parts or not directory.is_relative_to(root):
        raise PreviewError("private_output")
    current = ROOT
    for part in directory.relative_to(ROOT).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PreviewError("private_output")
        if current.is_relative_to(root) and info.st_mode & 0o077:
            raise PreviewError("private_output")
        if info.st_mode & 0o022:
            raise PreviewError("private_output")
    return directory


def save(result, out_dir: Path):
    directory = private_dir(out_dir)
    destination = directory / (result.fingerprint + ".json")
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", dir=directory) as tmp:
        metadata = result.public_report() | {
            "format": result.format,
            "mapping": asdict(result.mapping),
            "passport": asdict(result.passport),
            "messages": {code: MESSAGES[code] for code in result.summary.reason_counts},
        }
        tmp.write('{"delivery": ')
        json.dump(metadata, tmp, ensure_ascii=False, sort_keys=True)
        tmp.write(', "rows": [\n')
        for index, row in enumerate(result.rows):
            if index:
                tmp.write(",\n")
            item = asdict(row) | {"eligible_for": row.eligible_for}
            json.dump(item, tmp, ensure_ascii=False, sort_keys=True)
        tmp.write("\n]}\n")
        tmp.flush()
        try:
            os.link(tmp.name, destination)
        except FileExistsError:
            info = destination.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
                or info.st_size != tmp.tell()
            ):
                raise PreviewError("private_output") from None
            with destination.open("rb") as old, open(tmp.name, "rb") as new:
                if (
                    hashlib.file_digest(old, "sha256").digest()
                    != hashlib.file_digest(new, "sha256").digest()
                ):
                    raise PreviewError("private_output") from None


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    for flag in ("input", "mapping", "passport"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--format", choices=("csv", "json"), required=True)
    parser.add_argument("--out-dir", type=Path)
    try:
        args = parser.parse_args(argv)
        result = preview(
            read_local(args.input, MAX_BYTES),
            args.format,
            read_local(args.mapping, MAX_CONFIG),
            read_local(args.passport, MAX_CONFIG),
        )
        if args.out_dir is not None:
            save(result, args.out_dir)
        print(json.dumps(result.public_report(), sort_keys=True))
        return 0
    except (PreviewError, OSError) as exc:
        code = exc.code if isinstance(exc, PreviewError) else "io_error"
        print(
            json.dumps({"error": code, "message": MESSAGES[code]}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
