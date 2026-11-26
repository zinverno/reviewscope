"""Parameterized, transaction-scoped runtime queries. The pool belongs to one ASGI lifespan."""

from contextlib import contextmanager
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from . import PRIVATE_BINDS_SAFE
from .ledger import SCOPED, LedgerError, gate
from .security import ROLES, permits

METADATA = {
    "sources": ("source", "id,kind"),
    "snapshots": ("snapshot", "id,source_id,created_at"),
    "runs": ("analysis_run", "id,snapshot_id,status"),
}



def pool():
    # libpq handles the operator's service/passfile. Always authenticate as this exact login.
    return create_engine(
        "postgresql+psycopg://",
        connect_args={
            "user": "rs_runtime",
            "connect_timeout": 3,
            "options": "-c search_path=pg_catalog,public",
        },
        pool_size=2,
        max_overflow=0,
        pool_timeout=4,
        hide_parameters=True,
    )


@contextmanager
def transaction(engine):
    try:
        with engine.begin() as connection:
            with connection.connection.driver_connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT (current_user=session_user AND current_user='rs_runtime'
                    AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)
                    AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid)
                    AND NOT has_schema_privilege(current_user,'public','CREATE')
                    AND NOT has_database_privilege(current_user,current_database(),'CREATE,TEMP')
                    AND (SELECT array_agg(version_num) FROM public.alembic_version)
                        IN (ARRAY['0006_retention']::varchar[], ARRAY['0007_browser_delivery']::varchar[], ARRAY['0008_review_evidence']::varchar[], ARRAY['0009_overview']::varchar[], ARRAY['0010_signals']::varchar[], ARRAY['0011_reviewer_context']::varchar[], ARRAY['0012_candidate_search']::varchar[], ARRAY['0013_reviewer_context_plan']::varchar[], ARRAY['0014_aspects']::varchar[], ARRAY['0015_rerank']::varchar[], ARRAY['0016_aspect_decisions']::varchar[], ARRAY['0017_human_review']::varchar[], ARRAY['0018_typed_relations']::varchar[], ARRAY['0019_topic_discovery']::varchar[])
                    AND (SELECT bool_and(row_security_active(t::regclass)) FROM unnest(%s::text[]) t)
                    AND (to_regclass('public.browser_delivery') IS NULL OR
                        (row_security_active(to_regclass('public.browser_delivery'))
                        AND row_security_active(to_regclass('public.browser_admission'))
                        AND row_security_active(to_regclass('public.browser_summary'))
                        AND NOT has_any_column_privilege(current_user,to_regclass('public.browser_delivery'),'SELECT')
                        AND NOT has_any_column_privilege(current_user,to_regclass('public.browser_admission'),'SELECT')
                        AND NOT has_any_column_privilege(current_user,'public.review_version','SELECT')
                        AND NOT has_any_column_privilege(current_user,'public.import_row','SELECT')))
                    AND NOT EXISTS(SELECT FROM pg_class c WHERE c.relnamespace='public'::regnamespace
                        AND c.relname IN ('aspect','aspect_version','decision_binding','decision_row',
                    'human_review_event','human_review_plan','human_plan_member','human_review_result','human_result_member','relation_result','relation_pair','topic_result','topic_input','topic_event','topic_candidate','topic_history','topic_request')
                    AND (NOT row_security_active(c.oid)
                        OR has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                    OR has_table_privilege(current_user,c.oid,'DELETE,TRUNCATE,TRIGGER')))
                    AND NOT EXISTS(SELECT FROM pg_attribute a WHERE a.attrelid='analysis_run'::regclass
                        AND a.attname='candidate_params' AND has_column_privilege(current_user,a.attrelid,a.attnum,'SELECT'))
                    AND to_regclass('public.password_credential') IS NOT NULL
                    AND to_regclass('public.access_session') IS NOT NULL
                    AND to_regclass('public.principal') IS NOT NULL) AS ready
                    FROM pg_roles r WHERE rolname=current_user""",
                    (SCOPED,),
                )
                if not cur.fetchone()["ready"]:
                    raise HTTPException(503)
                gate(cur)
                yield cur
    except (psycopg.Error, SQLAlchemyError, LedgerError):
        raise HTTPException(503) from None


def identify(cur, proof: str):
    cur.execute("SELECT set_config('rs.session_proof',%s,true)", (proof,))
    cur.execute("SELECT access_actor() AS id")
    principal = cur.fetchone()["id"]
    if principal is None:
        raise HTTPException(401)
    return principal


def workspaces(cur, principal, offset):
    cur.execute(
        """SELECT w.id,w.name,w.state,m.role FROM workspace w
        JOIN workspace_membership m ON w.id=m.workspace_id
        WHERE m.principal_id=%s AND m.state='active' AND m.role=ANY(%s) ORDER BY w.id LIMIT 50 OFFSET %s""",
        (principal, sorted(ROLES["read"]), offset),
    )
    return cur.fetchall()


def authorize(cur, principal, workspace, action):
    cur.execute(
        """SELECT role FROM workspace_membership
        WHERE workspace_id=%s AND principal_id=%s AND state='active'""",
        (workspace, principal),
    )
    member = cur.fetchone()
    if member is None:
        raise HTTPException(404)
    if not permits(member["role"], action):
        raise HTTPException(403)
    cur.execute("SELECT set_config('rs.workspace',%s,true)", (str(workspace),))


def metadata(cur, resource, workspace, object_id: UUID | None, offset):
    table, fields = METADATA[resource]  # Static identifiers, selected by registered endpoint.
    if object_id is None:
        cur.execute(
            f"SELECT {fields} FROM {table} WHERE workspace_id=%s ORDER BY id LIMIT 50 OFFSET %s",
            (workspace, offset),
        )
        return cur.fetchall()
    cur.execute(
        f"SELECT {fields} FROM {table} WHERE workspace_id=%s AND id=%s", (workspace, object_id)
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404)
    return row


def job_status(cur, workspace, *, run=None, snapshot=None):
    # A published artifact's read policy also checks the run's workspace policy.
    condition = (
        "j.id=%s"
        if run is not None
        else (
            "j.snapshot_id=%s AND j.job_kind='snapshot_integrity_and_summary_v1' AND j.status='succeeded'"
        )
    )
    cur.execute(
        f"""SELECT j.id,j.snapshot_id,j.status,j.job_kind,j.engine_version,j.definition_version,
            j.model_version,j.attempt,j.stage,j.processed,j.total,j.reason,j.cancel_requested,
            j.finished_at,a.id AS artifact_id,a.checksum AS sha256,a.size_bytes
            FROM analysis_run j LEFT JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=
                (j.workspace_id,j.id,j.output_artifact_id)
            WHERE j.workspace_id=%s AND j.stage IS NOT NULL AND {condition}
            ORDER BY j.finished_at DESC,j.id DESC LIMIT 1""",
        (workspace, run if run is not None else snapshot),
    )
    value = cur.fetchone()
    if value is None:
        raise HTTPException(404)
    return value


def private_binds(cur):
    # Check before sending the private bind value to PostgreSQL.
    cur.execute(PRIVATE_BINDS_SAFE)
    if not cur.fetchone()["safe"]:
        raise HTTPException(503)
