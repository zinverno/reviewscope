"""Register one locally authorized canonical delivery using the verified PR-09 inputs."""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from operational.delivery_objects import (
    DeliveryError,
    encoded,
    object_directory,
    private_directory,
    publish,
    verify_file,
)
from reviewscope.access.ledger import LedgerError, certified_space, publishing
from reviewscope.import_preview import preview
from reviewscope.import_preview.contract import MAX_BYTES, MAX_CONFIG, VERSION, PreviewError
from reviewscope.import_preview.normalize import event_time
from reviewscope.import_preview.reader import JsonNumber, load_json, read_local, records

FORMAT = "canonical-json-v1"


def coverage_passport(data):
    value = load_json(data)
    required = {
        "coverage",
        "filters",
        "mode",
        "basis",
        "covered_from",
        "covered_until",
        "received_at",
    }
    if (
        not isinstance(value, dict)
        or value.keys() != required
        or value["coverage"] not in (None, "unknown", "partial", "full")
        or value["mode"] not in (None, "period", "cumulative")
        or not isinstance(value["filters"], (dict, type(None)))
        or not isinstance(value["basis"], (str, type(None)))
    ):
        raise DeliveryError("invalid_coverage")
    instants = {}
    for key in ("covered_from", "covered_until", "received_at"):
        if value[key] is not None:
            event, _ = event_time(value[key], None)
            if event.kind != "instant":
                raise DeliveryError("invalid_coverage")
            instants[key] = datetime.fromisoformat(event.value)
    if (
        "covered_from" in instants
        and "covered_until" in instants
        and instants["covered_from"] >= instants["covered_until"]
    ):
        raise DeliveryError("invalid_coverage")
    if value["coverage"] == "full" and (
        value["filters"] is None or value["mode"] is None or not (value["basis"] or "").strip()
    ):
        raise DeliveryError("missing_coverage_evidence")
    if (
        value["coverage"] == "full"
        and value["mode"] == "period"
        and not {"covered_from", "covered_until"} <= instants.keys()
    ):
        raise DeliveryError("missing_coverage_interval")
    return value


def authorize(conn, args, witness):
    row = conn.execute(
        """
        SELECT coalesce(s.external_source_id,s.source_key),w.delivery_object_root,a.principal_id
        FROM delivery_authorization a JOIN source s ON (s.workspace_id,s.id)=(a.workspace_id,a.source_id)
        JOIN workspace w ON w.id=a.workspace_id
        JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(a.workspace_id,a.principal_id)
        JOIN principal p ON p.id=m.principal_id
        WHERE a.workspace_id=%s AND a.source_id=%s AND current_user=session_user
          AND a.operator_login=session_user AND current_user<>'rs_runtime'
          AND w.state='active' AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active'
          AND (SELECT version_num FROM alembic_version) IN ('0006_retention','0007_browser_delivery')
        FOR UPDATE OF s FOR SHARE OF a,w,m,p
    """,
        (args.workspace, args.source),
    ).fetchone()
    if row is None or str(args.object_root) != row[1]:
        raise DeliveryError("scope_denied")
    certified_space(witness, args.workspace, args.object_root)
    private_directory(args.object_root)
    return row[0], row[2]


def version_for(conn, scope, batch, row, metadata):
    if row.source_review_id is None:
        (identity,) = conn.execute(
            """
            INSERT INTO review_identity(workspace_id,source_id,external_place_id,observation_batch_id,observation_position)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """,
            (*scope, row.source_place_id, batch, row.source_position),
        ).fetchone()
    else:
        key = (*scope, row.source_place_id, row.source_review_id)
        conn.execute(
            """INSERT INTO review_identity(workspace_id,source_id,external_place_id,external_review_id)
                        VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,source_id,external_place_id,external_review_id)
                        DO NOTHING""",
            key,
        )
        (identity,) = conn.execute(
            """SELECT id FROM review_identity WHERE
            (workspace_id,source_id,external_place_id,external_review_id)=(%s,%s,%s,%s)""",
            key,
        ).fetchone()
    last = conn.execute(
        """
        SELECT id,version_no,normalization_version,source_reviewer_id,text_content,rating,metadata
        FROM review_version WHERE (workspace_id,source_id,identity_id)=(%s,%s,%s)
        ORDER BY version_no DESC LIMIT 1
    """,
        (*scope, identity),
    ).fetchone()
    content = (VERSION, row.reviewer_id, row.text, row.rating_1_to_5, metadata)
    if last and last[2:] == content:
        return identity, last[0]
    day = row.event.value if row.event.kind == "date" else None
    instant = row.event.value if row.event.kind == "instant" else None
    published = row.event_basis == "published"
    (version,) = conn.execute(
        """
        INSERT INTO review_version(workspace_id,source_id,identity_id,batch_id,external_place_id,version_no,
            normalization_version,source_reviewer_id,text_content,rating,metadata,event_date,event_at,
            published_date,published_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """,
        (
            *scope,
            identity,
            batch,
            row.source_place_id,
            last[1] + 1 if last else 1,
            *content[:-1],
            Jsonb(metadata),
            day,
            instant,
            day if published else None,
            instant if published else None,
        ),
    ).fetchone()
    return identity, version


def journal(conn, scope, batch, result, data):
    p = result.passport
    encoding = "cp1251" if p.encoding == "cp1251" else "utf-8-sig"
    inputs = records(data, result.format, encoding, p.delimiter, result.mapping.fields)
    # Observation time, coverage, byte layout and delivery ID do not constitute review corrections.
    rules = hashlib.sha256(
        encoded(
            {
                "profile": result.mapping.version,
                "mapped_fields": {
                    k: v for k, v in result.mapping.fields.items() if v and v != "observed_at"
                },
                "local_date": p.local_date,
                "timezone": p.timezone,
                "rating_scale": p.rating_scale,
                "reviewer_id_allowed": p.reviewer_id_allowed,
            }
        )
    ).hexdigest()
    event_key = next((k for k, v in result.mapping.fields.items() if v == "published_at"), None)
    accepted = {}
    expected = []
    for row, (record, _, _) in zip(result.rows, inputs, strict=True):
        presence = {
            target: "null" if record[key] is None else "present"
            for key, target in result.mapping.fields.items()
            if target and key in record
        }
        links = (None, None)
        if row.status == "accepted":
            metadata = {
                "rules": rules,
                "event": asdict(row.event),
                "event_basis": row.event_basis,
                "rating_raw": row.rating_raw,
                "rating_scale": p.rating_scale,
                "source_url": row.source_url,
            }
            raw_event = record.get(event_key)
            if row.event.kind == "invalid" and raw_event is not None:
                metadata["invalid_event_input"] = (
                    {"number": raw_event.raw} if isinstance(raw_event, JsonNumber) else raw_event
                )
            links = version_for(conn, scope, batch, row, metadata)
            accepted[row.source_position] = links
        elif row.status == "confirmed_duplicate":
            links = accepted[row.related_position]
        expected.append(
            (
                row.source_position,
                row.status,
                row.reasons,
                row.warnings,
                row.related_position,
                *links,
                row.field_provenance,
                presence,
            )
        )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO import_row(workspace_id,source_id,batch_id,position,status,reasons,
            warnings,related_position,identity_id,version_id,provenance,field_presence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                (*scope, batch, *entry[:-2], Jsonb(entry[-2]), Jsonb(entry[-1]))
                for entry in expected
            ),
        )
    actual = conn.execute(
        """SELECT position,status,reasons,warnings,related_position,identity_id,version_id,
        provenance,field_presence FROM import_row WHERE (workspace_id,source_id,batch_id)=(%s,%s,%s)
        ORDER BY position""",
        (*scope, batch),
    )
    if any(a != b for a, b in zip(actual, expected, strict=True)):
        raise DeliveryError("journal_mismatch")
    return sorted(version for _, version in accepted.values())


def snapshot_bytes(conn, scope, ids, filters):
    yield encoded(filters) + b"\n"
    # Stream the projection so the object size never determines process memory.
    with conn.cursor(name="canonical_versions", row_factory=dict_row) as cur:
        cur.itersize = 128
        cur.execute(
            """
            SELECT v.id::text AS version_id,i.id::text AS identity_id,i.external_place_id AS source_place_id,
                   i.external_review_id AS source_review_id,v.version_no,v.normalization_version AS normalization,
                   v.source_reviewer_id AS reviewer_id,v.text_content AS text,v.rating AS rating_1_to_5,v.metadata,v.content_sha256,
                   to_char(v.event_date,'YYYY-MM-DD') AS event_date,
                   to_char(v.event_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS event_utc,
                   to_char(v.published_date,'YYYY-MM-DD') AS published_date,
                   to_char(v.published_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS published_utc
            FROM (SELECT DISTINCT unnest(%s::uuid[]) AS id) selected
            JOIN LATERAL (SELECT * FROM review_version
                WHERE (workspace_id,source_id,id)=(%s,%s,selected.id) LIMIT 1) v ON true
            JOIN LATERAL (SELECT * FROM review_identity
                WHERE (workspace_id,source_id,id)=(v.workspace_id,v.source_id,v.identity_id) LIMIT 1) i ON true
            ORDER BY v.id
        """,
            (ids, *scope),
        )
        for row in cur:
            yield encoded(row) + b"\n"


def verify_snapshot(conn, scope, batch, directory):
    stored = conn.execute(
        """
        SELECT s.id,s.duckdb_sha256,s.filters,s.version_ids,
               s.manifest_sha256=snapshot_digest(s.workspace_id,s.source_id,s.batch_id,s.duckdb_sha256,s.filters,s.version_ids),
               ARRAY(SELECT m.version_id FROM snapshot_member m
                     WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(s.workspace_id,s.source_id,s.id) ORDER BY 1)
        FROM snapshot s WHERE (s.workspace_id,s.source_id,s.batch_id)=(%s,%s,%s) AND s.filters->>'format'=%s
    """,
        (*scope, batch, FORMAT),
    ).fetchone()
    if not stored or not stored[4] or stored[3] != stored[5]:
        raise DeliveryError("manifest_mismatch")
    digest = hashlib.sha256()
    for chunk in snapshot_bytes(conn, scope, stored[5], stored[2]):
        digest.update(chunk)
    if digest.hexdigest() != stored[1]:
        raise DeliveryError("manifest_mismatch")
    verify_file(directory, stored[1])
    return stored[0]


def register(conn, args, witness):
    source_key, principal = authorize(conn, args, witness)
    data = read_local(args.input, MAX_BYTES)
    mapping = read_local(args.mapping, MAX_CONFIG)
    passport = read_local(args.passport, MAX_CONFIG)
    coverage_bytes = read_local(args.coverage_passport, MAX_CONFIG)
    return register_bytes(
        conn, args, source_key, principal, data, mapping, passport, coverage_bytes
    )


def register_bytes(
    conn, args, source_key, principal, data, mapping, passport, coverage_bytes, *, browser=False
):
    """Both callers hold the exclusive source row lock for the whole delivery transaction."""
    scope = (args.workspace, args.source)
    batch = args.delivery_id
    coverage_sha = hashlib.sha256(coverage_bytes).hexdigest()
    cover = coverage_passport(coverage_bytes)
    result = preview(data, args.format, mapping, passport)
    if result.fingerprint != args.preview_fingerprint:
        raise DeliveryError("preview_mismatch")
    if browser:
        allowed = {
            place
            for (place,) in conn.execute(
                "SELECT external_place_id FROM source_location WHERE workspace_id=%s AND source_id=%s",
                scope,
            )
        }
        if not result.allowed_in_browser(source_key, allowed):
            raise DeliveryError("scope_denied")
    places = {r.source_place_id for r in result.rows if r.status == "accepted"}
    if result.passport.source_place_id:
        places.add(result.passport.source_place_id)
    mapped = conn.execute(
        """SELECT external_place_id FROM source_location WHERE
        workspace_id=%s AND source_id=%s AND external_place_id=ANY(%s)""",
        (*scope, list(places)),
    )
    if (
        places != {p for (p,) in mapped}
        or result.passport.source not in (None, source_key)
        or any(r.source != source_key for r in result.rows if r.status == "accepted")
    ):
        raise DeliveryError("scope_denied")
    delivery = {
        "fingerprint": result.fingerprint,
        "mapping_sha256": result.mapping_sha256,
        "passport_sha256": result.passport_sha256,
        "coverage_sha256": coverage_sha,
        "format": result.format,
        "coverage_passport": cover,
    }
    filters = {
        "format": FORMAT,
        "analysis": "ANALYSIS_NOT_AVAILABLE",
        "selection": "accepted_in_delivery",
        "workspace": str(scope[0]),
        "source": str(scope[1]),
        "batch": str(batch),
        "observed_at": asdict(result.passport.observed_at) if result.passport.observed_at else None,
        "coverage_passport": cover,
        "preview_fingerprint": result.fingerprint,
    }
    summary = result.summary
    counts = (
        summary.total_input_rows,
        summary.accepted,
        summary.quarantined,
        summary.confirmed_duplicates,
    )
    report = {
        "input": counts[0],
        "accepted": counts[1],
        "quarantined": counts[2],
        "confirmed_duplicates": counts[3],
    }
    prior = conn.execute(
        """SELECT delivery,file_sha256 FROM import_batch
        WHERE (workspace_id,source_id,id)=(%s,%s,%s)""",
        (*scope, batch),
    ).fetchone()
    blobs = (
        (data, result.file_sha256),
        (mapping, result.mapping_sha256),
        (passport, result.passport_sha256),
        (coverage_bytes, coverage_sha),
    )
    if prior:
        if prior != (delivery, result.file_sha256):
            raise DeliveryError("delivery_conflict")
        directory = object_directory(args.object_root, scope[0], create=False)
        for _, digest in blobs:
            verify_file(directory, digest)
        snap = verify_snapshot(conn, scope, batch, directory)
        return report | {"outcome": "already_applied", "snapshot_id": str(snap)}
    if not args.apply:
        return report | {"outcome": "planned"}
    directory = object_directory(args.object_root, scope[0], create=True)
    for blob, digest in blobs:
        publish(directory, (blob,), digest)
    conn.execute(
        """INSERT INTO import_batch(workspace_id,source_id,id,file_sha256,procedure_version,status,
        received_at,covered_from,covered_until,coverage,input_count,accepted_count,rejected_count,
        confirmed_duplicate_count,delivery,registered_by) VALUES (%s,%s,%s,%s,%s,'accepted',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            *scope,
            batch,
            result.file_sha256,
            VERSION,
            cover["received_at"],
            cover["covered_from"],
            cover["covered_until"],
            cover["coverage"],
            *counts,
            Jsonb(delivery),
            principal,
        ),
    )
    ids = journal(conn, scope, batch, result, data)
    digest = publish(directory, snapshot_bytes(conn, scope, ids, filters))
    conn.execute(
        """INSERT INTO snapshot(workspace_id,source_id,batch_id,duckdb_sha256,filters,version_ids,manifest_sha256)
        VALUES (%s,%s,%s,%s,%s,%s,snapshot_digest(%s,%s,%s,%s,%s,%s))""",
        (*scope, batch, digest, Jsonb(filters), ids) * 2,
    )
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    for _, expected_sha in blobs:
        verify_file(directory, expected_sha)
    snap = verify_snapshot(conn, scope, batch, directory)
    return report | {"outcome": "applied", "snapshot_id": str(snap)}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DeliveryError("invalid_arguments")


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    for field in ("workspace", "source", "delivery-id"):
        parser.add_argument("--" + field, type=UUID, required=True)
    for field in ("input", "mapping", "passport", "coverage-passport", "object-root"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--format", choices=("csv", "json"), required=True)
    parser.add_argument("--preview-fingerprint", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("dry-run", "apply"):
        mode.add_argument("--" + flag, action="store_true")
    try:
        args = parser.parse_args(argv)
        socket = os.environ.get("PGHOST", "")
        if (
            not socket.startswith("/")
            or "," in socket
            or os.environ.get("PGHOSTADDR")
            or os.environ.get("PGSERVICE")
        ):
            raise DeliveryError("local_socket_required")
        with psycopg.connect(host=socket, options="-c search_path=pg_catalog,public") as conn:
            with conn.cursor() as cur, publishing(cur) as witness:
                report = register(conn, args, witness)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (DeliveryError, PreviewError, OSError, ValueError, psycopg.Error) as exc:
        code = (
            str(exc)
            if isinstance(exc, (DeliveryError, LedgerError))
            else exc.code
            if isinstance(exc, PreviewError)
            else "delivery_refused"
        )
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
