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
          AND (SELECT version_num FROM alembic_version) IN ('0006_retention','0007_browser_delivery','0008_review_evidence','0009_overview')
        FOR UPDATE OF s FOR SHARE OF a,w,m,p
    """,
        (args.workspace, args.source),
    ).fetchone()
    if row is None or str(args.object_root) != row[1]:
        raise DeliveryError("scope_denied")
    certified_space(witness, args.workspace, args.object_root)
    private_directory(args.object_root)
    return row[0], row[2]


def version_links(conn, scope, batch, rows):
    """The source row is locked; compare each accepted position to its latest immutable version."""
    links = {}
    for offset in range(0, len(rows), 256):
        group = rows[offset : offset + 256]
        keys = [dict(pos=r["pos"], place=r["place"], review=r["review"]) for r in group]
        conn.execute(
            """INSERT INTO review_identity(workspace_id,source_id,external_place_id,external_review_id,
                  observation_batch_id,observation_position)
               SELECT %s,%s,place,review,CASE WHEN review IS NULL THEN %s::uuid END,
                      CASE WHEN review IS NULL THEN pos END
               FROM jsonb_to_recordset(%s) r(pos integer,place text,review text)
               ON CONFLICT(workspace_id,source_id,external_place_id,external_review_id) DO NOTHING""",
            (*scope, batch, Jsonb(keys)),
        )
        result = conn.execute(
            """WITH input AS MATERIALIZED (
                SELECT * FROM jsonb_to_recordset(%(rows)s) r(pos integer,place text,review text,
                  author text,body text,rating smallint,meta jsonb,day date,instant timestamptz,published boolean)
            ), identities AS MATERIALIZED (
                SELECT r.pos,i.id FROM input r JOIN review_identity i ON
                 (i.workspace_id,i.source_id,i.external_place_id,i.external_review_id)
                  =(%(w)s,%(s)s,r.place,r.review)
                UNION ALL
                SELECT r.pos,i.id FROM input r JOIN review_identity i ON
                 (i.workspace_id,i.source_id,i.observation_batch_id,i.observation_position)
                  =(%(w)s,%(s)s,%(batch)s,r.pos) WHERE r.review IS NULL
            ), resolved AS MATERIALIZED (
                SELECT r.*,i.id identity_id,v.id previous_id,coalesce(v.version_no,0)+1 next_version,
                 v.id IS NOT NULL AND
                 ROW(v.normalization_version,v.source_reviewer_id,v.text_content,v.rating,v.metadata)
                 IS NOT DISTINCT FROM ROW(%(norm)s::text,r.author,r.body,r.rating,r.meta) AS unchanged
                FROM input r JOIN identities i USING(pos)
                LEFT JOIN LATERAL (SELECT * FROM review_version v
                 WHERE (v.workspace_id,v.source_id,v.identity_id)=(%(w)s,%(s)s,i.id)
                 ORDER BY v.version_no DESC LIMIT 1) v ON true
            ), added AS (
                INSERT INTO review_version(workspace_id,source_id,identity_id,batch_id,external_place_id,
                 version_no,normalization_version,source_reviewer_id,text_content,rating,metadata,
                 event_date,event_at,published_date,published_at)
                SELECT %(w)s,%(s)s,identity_id,%(batch)s,place,next_version,%(norm)s,author,body,rating,
                 meta,day,instant,CASE WHEN published THEN day END,CASE WHEN published THEN instant END
                FROM resolved WHERE NOT unchanged RETURNING identity_id,id
            )
            SELECT r.pos,r.identity_id,coalesce(a.id,r.previous_id) FROM resolved r
            LEFT JOIN added a USING(identity_id)""",
            dict(w=scope[0], s=scope[1], batch=batch, norm=VERSION, rows=Jsonb(group)),
        ).fetchall()
        if len(result) != len(group) or any(v is None for _, _, v in result):
            raise DeliveryError("journal_mismatch")
        links.update((pos, (i, v)) for pos, i, v in result)
    return links


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
    versions = []
    positions = []
    for row, (record, _, _) in zip(result.rows, inputs, strict=True):
        presence = {
            target: "null" if record[key] is None else "present"
            for key, target in result.mapping.fields.items()
            if target and key in record
        }
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
            versions.append(
                dict(
                    pos=row.source_position,
                    place=row.source_place_id,
                    review=row.source_review_id,
                    author=row.reviewer_id,
                    body=row.text,
                    rating=row.rating_1_to_5,
                    meta=metadata,
                    day=row.event.value if row.event.kind == "date" else None,
                    instant=row.event.value if row.event.kind == "instant" else None,
                    published=row.event_basis == "published",
                )
            )
        positions.append((row, presence))
    accepted = version_links(conn, scope, batch, versions)
    expected = []
    for row, presence in positions:
        linked = (
            row.related_position if row.status == "confirmed_duplicate" else row.source_position
        )
        identity, version = accepted.get(linked, (None, None))
        expected.append(
            (
                row.source_position,
                row.status,
                row.reasons,
                row.warnings,
                row.related_position,
                identity,
                version,
                row.field_provenance,
                presence,
            )
        )
    names = (
        "position",
        "status",
        "reasons",
        "warnings",
        "related_position",
        "identity_id",
        "version_id",
        "provenance",
        "field_presence",
    )
    for offset in range(0, len(expected), 256):
        entries = []
        for entry in expected[offset : offset + 256]:
            fields = dict(zip(names, entry, strict=True))
            for name in ("identity_id", "version_id"):
                if fields[name] is not None:
                    fields[name] = str(fields[name])
            entries.append(fields)
        conn.execute(
            """INSERT INTO import_row(workspace_id,source_id,batch_id,position,status,reasons,
                warnings,related_position,identity_id,version_id,provenance,field_presence)
               SELECT %s,%s,%s,r.* FROM jsonb_to_recordset(%s) r(
                position integer,status text,reasons text[],warnings text[],related_position integer,
                identity_id uuid,version_id uuid,provenance jsonb,field_presence jsonb)""",
            (*scope, batch, Jsonb(entries)),
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
            WITH selected AS MATERIALIZED (SELECT DISTINCT unnest(%s::uuid[]) id),
            versions AS MATERIALIZED (
                SELECT v.* FROM review_version v JOIN selected USING(id)
                WHERE v.workspace_id=%s AND v.source_id=%s
            ), identities AS MATERIALIZED (
                SELECT i.id,i.external_place_id,i.external_review_id FROM review_identity i
                JOIN (SELECT DISTINCT identity_id FROM versions) k ON k.identity_id=i.id
                WHERE i.workspace_id=%s AND i.source_id=%s
            )
            SELECT v.id::text AS version_id,i.id::text AS identity_id,i.external_place_id AS source_place_id,
                   i.external_review_id AS source_review_id,v.version_no,v.normalization_version AS normalization,
                   v.source_reviewer_id AS reviewer_id,v.text_content AS text,v.rating AS rating_1_to_5,v.metadata,v.content_sha256,
                   to_char(v.event_date,'YYYY-MM-DD') AS event_date,
                   to_char(v.event_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS event_utc,
                   to_char(v.published_date,'YYYY-MM-DD') AS published_date,
                   to_char(v.published_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS published_utc
            FROM versions v JOIN identities i ON i.id=v.identity_id
            ORDER BY v.id
        """,
            (ids, *scope, *scope),
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
