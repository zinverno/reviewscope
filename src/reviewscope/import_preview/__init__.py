"""Canonical local preview, independent of the legacy DataSource and both stores."""

import hashlib
from collections import defaultdict
from dataclasses import asdict, dataclass

from .contract import VERSION, CanonicalRow, ImportSummary, PreviewError
from .normalize import MappingProfile, Passport, configuration, normalize
from .reader import records


@dataclass(slots=True)
class ImportPreview:
    fingerprint: str
    file_sha256: str
    mapping_sha256: str
    passport_sha256: str
    format: str
    mapping: MappingProfile
    passport: Passport
    rows: list[CanonicalRow]
    summary: ImportSummary

    def public_report(self):
        return {
            "procedure_version": VERSION,
            "verification": "REAL_FORMAT_NOT_VERIFIED",
            "fingerprint": self.fingerprint,
            "file_sha256": self.file_sha256,
            "mapping_sha256": self.mapping_sha256,
            "passport_sha256": self.passport_sha256,
            **asdict(self.summary),
        }


def preview(data: bytes, fmt: str, mapping: bytes, passport: bytes) -> ImportPreview:
    profile, source, zone = configuration(fmt, mapping, passport)
    rows = []
    groups = defaultdict(list)
    encoding = "cp1251" if source.encoding == "cp1251" else "utf-8-sig"
    for position, (record, errors, raw) in enumerate(
        records(data, fmt, encoding, source.delimiter, profile.fields), 1
    ):
        row = normalize(position, record, errors, profile, source, zone)
        rows.append(row)
        if row.source_review_id is not None and row.source and row.source_place_id:
            # Compare every input cell, including ignored columns and typed JSON number tokens.
            groups[row.source, row.source_place_id, row.source_review_id].append((row, raw))
    if not rows:
        raise PreviewError("empty_input")
    for group in groups.values():
        if len(group) == 1:
            continue
        first, original = group[0]
        witness = next((row for row, raw in group[1:] if raw != original), None)
        if witness is not None:
            for row, raw in group:
                row.quarantine("conflicting_id")
                row.related_position = (witness if raw == original else first).source_position
        elif all(row.status == "accepted" for row, _ in group):
            for row, _ in group[1:]:
                row.status = "confirmed_duplicate"
                row.reasons.append("confirmed_duplicate")
                row.related_position = first.source_position
    hashes = [hashlib.sha256(blob).hexdigest() for blob in (data, mapping, passport)]
    fingerprint = hashlib.sha256("|".join([VERSION, fmt, *hashes]).encode()).hexdigest()
    return ImportPreview(
        fingerprint, *hashes, fmt, profile, source, rows, ImportSummary.count(rows)
    )
