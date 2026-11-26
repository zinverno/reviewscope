"""Local PostgreSQL worker. Run from the checkout with python -m operational.worker."""

import argparse
import hashlib
import json
import os
import signal
import sys
from collections import Counter
from functools import partial
from pathlib import Path
from threading import Event
from time import monotonic

import psycopg

from operational import candidate_worker, decision_worker, rerank_worker, signals
from operational.delivery_objects import (
    DeliveryError,
    encoded,
    object_directory,
    open_object,
    publish,
    verify_file,
)
from operational.import_delivery import FORMAT, coverage_passport
from reviewscope.access.ledger import certified_space, gate, publishing
from reviewscope.import_preview.contract import MAX_BYTES, MAX_CONFIG, MAX_ROWS, MESSAGES, VERSION
from reviewscope.import_preview.normalize import configuration, stars

KIND = "snapshot_integrity_and_summary_v1"


class JobError(Exception):
    """An allowlisted failure at a trusted computation boundary."""


class Finished(Exception):
    """The DB closed, cancelled or fenced the attempt."""


def require(condition, code="INTEGRITY_ERROR"):
    if not condition:
        raise JobError(code)


def connect():
    host = os.environ.get("PGHOST", "")
    require(
        host.startswith("/")
        and "," not in host
        and not os.environ.get("PGHOSTADDR")
        and not os.environ.get("PGSERVICE"),
        "ACCESS_REVOKED",
    )
    conn = psycopg.connect(
        user="rs_worker",
        host=host,
        autocommit=True,
        connect_timeout=3,
        options="-c search_path=pg_catalog,public -c statement_timeout=10000",
    )
    try:
        (ready,) = conn.execute("""SELECT current_user=session_user AND current_user='rs_worker'
            AND NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
            AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid)
            AND NOT has_schema_privilege(current_user,'public','CREATE')
            AND NOT has_database_privilege(current_user,current_database(),'CREATE,TEMP')
            AND row_security_active('analysis_run') AND row_security_active('review_version')
            AND NOT has_table_privilege(current_user,'analysis_run','INSERT,UPDATE')
            AND NOT EXISTS(SELECT FROM pg_class c WHERE c.relnamespace='public'::regnamespace
                AND c.relname IN ('aspect','aspect_version','decision_binding','decision_row',
                    'human_review_event','human_review_plan','human_plan_member','human_review_result','human_result_member','monitor','monitor_version','monitor_archive','monitor_run','monitor_receipt','monitor_reuse')
                    AND (NOT row_security_active(c.oid)
                OR has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                    OR has_table_privilege(current_user,c.oid,'DELETE,TRUNCATE,TRIGGER')))
            AND (SELECT version_num FROM alembic_version) IN ('0006_retention','0007_browser_delivery','0008_review_evidence','0009_overview','0010_signals','0011_reviewer_context','0012_candidate_search','0013_reviewer_context_plan','0014_aspects','0015_rerank','0016_aspect_decisions','0017_human_review','0018_typed_relations','0019_topic_discovery','0020_topic_lock_order','0021_saved_monitors')
            FROM pg_roles r WHERE rolname=current_user""").fetchone()
        require(ready, "ACCESS_REVOKED")
        with conn.transaction(), conn.cursor() as cur:
            gate(cur)
        return conn
    except BaseException:
        conn.close()
        raise


def read_source(directory, digest, limit, pulse, *, keep=False):
    value = bytearray()
    actual = hashlib.sha256()
    size = 0
    with open_object(directory, digest) as stream:
        require(os.fstat(stream.fileno()).st_size <= limit, "RESOURCE_LIMIT")
        while block := stream.read(65536):
            size += len(block)
            require(size <= limit, "RESOURCE_LIMIT")
            actual.update(block)
            if keep:
                value.extend(block)
            pulse()
    require(actual.hexdigest() == digest)
    return bytes(value)


def measure(count, denominator):
    return {"numerator": count, "denominator": denominator}


def summarize(conn, claim, info, directory, pulse):
    w, r, _token = claim
    filters = info["filters"]
    require(filters.get("format") == FORMAT, "UNSUPPORTED_FORMAT")
    is_decision = info["kind"] == decision_worker.KIND
    is_candidate = info["kind"] == candidate_worker.KIND
    is_rerank = info["kind"] == rerank_worker.KIND
    is_signals = info["kind"] == signals.KIND
    require(
        (info["kind"], info["version"], info["definition"], info["model"], info["procedure"])
        in {
            (KIND, "1", "summary-1", "unused", VERSION),
            (signals.KIND, "1", "signals-1", "unused", VERSION),
            (decision_worker.KIND, "1", decision_worker.SCHEMA, decision_worker.PROFILE, VERSION),
            (rerank_worker.KIND, "1", rerank_worker.SCHEMA, rerank_worker.PROFILE, VERSION),
            (candidate_worker.KIND, "1", "candidate-search-1", "unused", VERSION),
            (candidate_worker.KIND, "1", "candidate-search-1", "minilm-pr04", VERSION),
        },
        "UNSUPPORTED_VERSION",
    )
    ids = info["ids"]
    require(ids is not None and info["input"] <= MAX_ROWS, "RESOURCE_LIMIT")
    total = len(ids)
    require(
        info["valid"]
        and info["accepted"] == total
        and info["input"] == total + info["rejected"] + info["duplicates"]
    )
    receipt = info["delivery"]
    require(
        filters["workspace"] == str(w)
        and filters["source"] == info["source"]
        and filters["batch"] == info["batch"]
        and filters["selection"] == "accepted_in_delivery"
        and filters["analysis"] == "ANALYSIS_NOT_AVAILABLE"
    )
    hashes = {k: receipt[k + "_sha256"] for k in ("mapping", "passport", "coverage")}
    hashes["file"] = info["file_sha256"]
    denominator = info["decision"]["eligible"] if is_decision else total
    verify = partial(pulse, "verify", 0, denominator)
    verify()
    read_source(directory, hashes["file"], MAX_BYTES, verify)
    mapping, passport, coverage = [
        read_source(directory, hashes[k], MAX_CONFIG, verify, keep=True)
        for k in ("mapping", "passport", "coverage")
    ]
    _, profile, _ = configuration(receipt["format"], mapping, passport)
    cover = coverage_passport(coverage)
    fingerprint = hashlib.sha256(
        "|".join(
            [VERSION, receipt["format"], hashes["file"], hashes["mapping"], hashes["passport"]]
        ).encode()
    ).hexdigest()
    require(
        receipt["fingerprint"] == filters["preview_fingerprint"] == fingerprint
        and cover == filters["coverage_passport"] == receipt["coverage_passport"]
        and cover["coverage"] == info["coverage"]
    )
    projected = []
    projection_bytes = 0
    counts = Counter()
    ratings = Counter()
    exclusions = Counter()
    reasons = info["reasons"] or {}
    require(set(reasons) <= MESSAGES.keys())
    actual = hashlib.sha256()
    with open_object(directory, info["snapshot_sha256"]) as stream:
        require(os.fstat(stream.fileno()).st_size <= 64 * 1024**2, "RESOURCE_LIMIT")

        def compare(value):
            line = encoded(value) + b"\n"
            require(stream.read(len(line)) == line)
            actual.update(line)

        compare(filters)
        n = 0
        while n < total:
            pulse("summarize", 0 if is_decision else n, denominator)
            rows = conn.execute(
                "SELECT * FROM job_versions(%s,%s,%s,%s)", (*claim, ids[n - 1] if n else None)
            ).fetchall()
            if not rows:
                pulse("summarize", 0 if is_decision else n, denominator)
                raise JobError("INTEGRITY_ERROR")
            for (row,) in rows:
                require(
                    n < total and row["version_id"] == ids[n] and row["normalization"] == VERSION
                )
                compare(row)
                meta = row["metadata"]
                rate, issue = stars(meta["rating_raw"], profile.rating_scale)
                require(
                    row["rating_1_to_5"] == rate
                    and meta["rating_scale"] == profile.rating_scale
                    and meta["event_basis"] == profile.event_basis
                )
                counts["unknown_author"] += row["reviewer_id"] is None
                counts["usable_text"] += bool((row["text"] or "").strip())
                if rate is None:
                    exclusions[issue] += 1
                else:
                    counts["known_rating"] += 1
                    ratings[rate] += 1
                kind = meta["event"]["kind"]
                if kind in ("date", "instant") and profile.event_basis in (
                    "published",
                    "submitted",
                ):
                    counts["comparable_date"] += 1
                else:
                    exclusions[
                        "date_" + kind
                        if kind in ("local", "invalid", "missing")
                        else "ineligible_event_basis"
                    ] += 1
                if is_candidate or is_rerank or is_decision:
                    projected.append((row["version_id"],row["content_sha256"],row["text"] or ""))
                if is_signals:
                    item = signals.projection(row, rate, issue)
                    projection_bytes += len(encoded(item))
                    if projection_bytes > signals.LIMIT:
                        pulse("summarize", n + 1, total)
                        raise JobError("RESOURCE_LIMIT")
                    projected.append(item)
                n += 1
        require(not stream.read(1) and actual.hexdigest() == info["snapshot_sha256"])
    if is_decision and info.get("monitor"):
        from operational import monitor_worker

        return monitor_worker.result(conn, claim, info, projected, directory, pulse)
    if is_decision:
        return decision_worker.result(conn, claim, info, projected, pulse)
    exclusions["missing_reviewer"] = counts["unknown_author"]
    exclusions["missing_text"] = total - counts["usable_text"]
    summary = {
        "schema": "snapshot-summary-1",
        "workspace_id": str(w),
        "run_id": str(r),
        "snapshot_id": info["snapshot_id"],
        "source_id": info["source"],
        "batch_id": info["batch"],
        "manifest_sha256": info["manifest"],
        "snapshot_sha256": info["snapshot_sha256"],
        "source_sha256": hashes,
        "preview_fingerprint": fingerprint,
        "job_kind": KIND,
        "engine_version": "1",
        "definition_version": "summary-1",
        "model_version": "unused",
        "parameters": {"selection": filters["selection"]},
        "coverage": info["coverage"],
        "coverage_mode": cover["mode"],
        "covered_from": cover["covered_from"],
        "covered_until": cover["covered_until"],
        "event_basis": profile.event_basis,
        "rating_scale": profile.rating_scale,
        "observations": measure(total, info["input"]),
        "quarantined": measure(info["rejected"], info["input"]),
        "confirmed_duplicates": measure(info["duplicates"], info["input"]),
        "delivery_exclusions": {k: measure(v, info["input"]) for k, v in reasons.items()},
        "eligibility": {
            k: measure(counts[k], total)
            for k in ("unknown_author", "usable_text", "known_rating", "comparable_date")
        },
        "metric_exclusions": {k: measure(v, total) for k, v in exclusions.items()},
        "rating_distribution": {
            str(k): measure(ratings[k], counts["known_rating"]) for k in range(1, 6)
        }
        if profile.rating_scale == [1, 5]
        else None,
        "limitations": [
            "DECLARED_COVERAGE_ONLY",
            "AUTHOR_METRICS_UNSUPPORTED",
            "ANALYSIS_NOT_AVAILABLE",
            "NO_PERIOD_COMPARISON",
        ],
    }
    if is_rerank:
        raw = read_source(
            directory,
            info["candidate"]["parent_sha256"],
            65536,
            partial(pulse, "summarize", total, total),
            keep=True,
        )
        summary = rerank_worker.result(
            summary,
            projected,
            info["candidate"],
            json.loads(raw),
            partial(pulse, "summarize", total, total),
        )
    if is_candidate:
        summary = candidate_worker.result(
            summary, projected, info["candidate"], partial(pulse, "summarize", total, total)
        )
    pulse("publish", total, total)
    return signals.result(summary, projected, info["as_of"]) if is_signals else summary


def linux_peak_kib(pid):
    # VmHWM starts at exec; getrusage can include the pre-exec pytest parent's footprint.
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def process(conn, job, stopping):
    claim, generation = job[:3], job[3]
    stage, processed, total = "verify", 0, None
    started = monotonic()

    def record(outcome):
        print(
            json.dumps(
                dict(
                    event="worker_stage",
                    stage=stage,
                    processed=processed,
                    attempt=generation,
                    outcome=outcome,
                    duration_ms=round((monotonic() - started) * 1000, 2),
                    peak_rss_kib=linux_peak_kib("self"),
                    db_backend_peak_rss_kib=linux_peak_kib(conn.info.backend_pid),
                )
            ),
            flush=True,
        )

    def step(problem=None, digest=None, size=None, manifest=None):
        return conn.execute(
            "SELECT worker_step(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (*claim, stage, processed, total, problem, digest, size, manifest),
        ).fetchone()[0]

    def pulse(next_stage, n, den):
        nonlocal stage, processed, total, started
        if next_stage != stage:
            processed = n
            record("completed")
            started = monotonic()
        stage, processed, total = next_stage, n, den
        state = step("WORKER_STOPPED" if stopping.is_set() else None)
        if state != "running":
            raise Finished(state)

    try:
        pulse(stage, processed, total)
        info = conn.execute("SELECT job_input(%s,%s,%s)", claim).fetchone()[0]
        if info is None:
            pulse(stage, processed, total)
            raise JobError("INTEGRITY_ERROR")
        require(info["filters"].get("format") == FORMAT, "UNSUPPORTED_FORMAT")
        require(info["root"] is not None, "ACCESS_REVOKED")
        with conn.transaction(), conn.cursor() as cur:
            certified_space(gate(cur), claim[0], info["root"])
        directory = object_directory(Path(info["root"]), claim[0], create=False)
        value = summarize(conn, claim, info, directory, pulse)
        if info["kind"] in (candidate_worker.KIND,rerank_worker.KIND):
            require(value.get("aspect") == info["candidate"].get("aspect"))
        data = encoded(value) + b"\n"
        require(len(data) <= signals.LIMIT, "RESOURCE_LIMIT")
        with conn.transaction(), conn.cursor() as cur, publishing(cur) as witness:
            certified_space(witness, claim[0], info["root"])
            available = conn.execute("SELECT job_input(%s,%s,%s) IS NOT NULL", claim).fetchone()[0]
            outcome = "running"
            if not available:
                # Commit the cancellation/revocation decision in this transaction.
                outcome = step("INTEGRITY_ERROR")
            elif info["kind"] in (candidate_worker.KIND,rerank_worker.KIND,decision_worker.KIND):
                outcome = step("WORKER_STOPPED" if stopping.is_set() else None)
            if outcome == "running":
                if info.get("monitor"):
                    from operational import monitor_worker

                    monitor_worker.checked_parent(conn, claim, info, directory, lambda: None)
                if info["kind"] == rerank_worker.KIND:
                    from reviewscope.access import PRIVATE_BINDS_SAFE

                    require(conn.execute(PRIVATE_BINDS_SAFE).fetchone()[0], "ACCESS_REVOKED")
                    parent = read_source(
                        directory,
                        info["candidate"]["parent_sha256"],
                        65536,
                        lambda: None,
                        keep=True,
                    )
                    conn.execute("SELECT set_config('rs.rerank_parent',%s,true)",(parent.decode(),))
                digest = publish(directory, (data,))
                verify_file(directory, digest, len(data))
                projection = {
                    signals.KIND: "rs.signals_projection",
                    decision_worker.KIND: "rs.decision_projection",
                    rerank_worker.KIND: "rs.candidate_projection",
                    candidate_worker.KIND: "rs.candidate_projection",
                }.get(info["kind"], "rs.summary_projection")
                conn.execute("SELECT set_config(%s,%s,true)", (projection, data.decode("utf-8")))
                outcome = step(
                    "WORKER_STOPPED" if stopping.is_set() else None,
                    digest,
                    len(data),
                    info["manifest"],
                )
    except Finished as exc:
        outcome = exc.args[0]
    except JobError as exc:
        outcome = step(exc.args[0])
    except psycopg.errors.QueryCanceled:
        outcome = step("RESOURCE_LIMIT")
    except psycopg.OperationalError:
        # If the connection itself was lost, the next worker recovers by lease expiry.
        outcome = step("STORAGE_UNAVAILABLE")
    except PermissionError:
        outcome = step("ACCESS_REVOKED")
    except OSError:
        outcome = step("STORAGE_UNAVAILABLE")
    except (DeliveryError, KeyError, TypeError, ValueError):
        outcome = step("INTEGRITY_ERROR")
    except Exception:
        outcome = step("INTERNAL_ERROR")
    record(outcome)
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and finish the claimed job")
    args = parser.parse_args()
    stopping = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        with connect() as conn:
            while not stopping.is_set():
                with conn.transaction(), conn.cursor() as cur:
                    gate(cur)
                    job = conn.execute("SELECT * FROM claim_job()").fetchone()
                if job:
                    process(conn, job, stopping)
                if args.once:
                    break
                if not job:
                    stopping.wait(1)
    except Exception:
        print('{"error":"worker_unavailable"}', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
