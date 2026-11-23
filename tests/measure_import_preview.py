"""Linux resource probe for the public summary path on a synthetic near-limit delivery."""

import json
import resource
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reviewscope.import_preview import preview  # noqa: E402
from reviewscope.import_preview.contract import MAX_BYTES, MAX_ROWS  # noqa: E402


def measure(fmt):
    resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2, 192 * 1024**2))
    fields = b'{"version":"resource-test","fields":{"id":"source_review_id","body":"text"}}'
    passport = {"source": "synthetic", "source_place_id": "p"}
    if fmt == "csv":
        passport["delimiter"] = ","
        header, footer, separator = b"id,body\n", b"\n", b"\n"
        template = b"%05d,%s"
    else:
        header, footer, separator = b"[", b"]", b","
        template = b'{"id":"%05d","body":"%s"}'
    base_size = len(header) + len(footer) + (MAX_ROWS - 1) * len(separator)
    remaining = MAX_BYTES - base_size - MAX_ROWS * len(template % (0, b""))
    width, extra = divmod(remaining, MAX_ROWS)
    data = (
        header
        + separator.join(template % (i, b"t" * (width + (i < extra))) for i in range(MAX_ROWS))
        + footer
    )
    assert len(data) == MAX_BYTES
    start = perf_counter()
    result = preview(data, fmt, fields, json.dumps(passport).encode())
    elapsed = perf_counter() - start
    assert result.summary.accepted == result.summary.total_input_rows == MAX_ROWS
    assert result.summary.warning_count == 3 * MAX_ROWS
    return {
        "format": fmt,
        "input_bytes": len(data),
        "seconds": round(elapsed, 3),
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "address_space_limit_mib": 192,
        **result.public_report(),
    }


if __name__ == "__main__":
    if sys.argv[1:] not in (["csv"], ["json"]):
        sys.exit("Choose csv or json.")
    print(json.dumps(measure(sys.argv[1]), sort_keys=True))
