"""Acquire only the public PR-21 evaluation assets. No corpus or credentials are needed."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

MANIFEST = Path(__file__).resolve().parents[1] / "src/reviewscope/embeddings/rerank_models.json"


def prepare(root, spec):
    directory = root / spec["revision"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, entry in spec["files"].items():
        dest = directory / name
        if dest.exists():
            with dest.open("rb") as stream:
                if (
                    dest.is_symlink()
                    or hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]
                ):
                    raise ValueError("existing model file differs")
            continue
        url = f"https://huggingface.co/{spec['model']}/resolve/{spec['revision']}/{name}?download=true"
        with NamedTemporaryFile(dir=directory) as tmp:
            total = 0
            digest = hashlib.sha256()
            with urlopen(url, timeout=60) as response:
                while block := response.read(1024**2):
                    total += len(block)
                    if total > entry["bytes"]:
                        raise ValueError("unexpected download size")
                    digest.update(block)
                    tmp.write(block)
            if total != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError("unverified model file")
            tmp.flush()
            os.link(tmp.name, dest)
        print(spec["model"], name, total, digest.hexdigest(), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    for spec in json.loads(MANIFEST.read_text()).values():
        prepare(args.directory, spec)
