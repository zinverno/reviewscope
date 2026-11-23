"""Add fenced background jobs without rewriting existing run identities."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0005_background_jobs"
down_revision = "0004_delivery_history"


def upgrade():
    conn = op.get_bind()
    if not conn.exec_driver_sql("""
        SELECT EXISTS (SELECT FROM pg_roles r WHERE rolname='rs_worker' AND rolcanlogin
            AND rolname <> current_user
            AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)
            AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid)
            AND NOT EXISTS (SELECT FROM pg_class WHERE relowner=r.oid)
            AND NOT EXISTS (SELECT FROM pg_database WHERE datdba=r.oid)
            AND NOT EXISTS (SELECT FROM pg_namespace WHERE nspowner=r.oid)
            AND NOT has_schema_privilege(rolname,'public','CREATE')
            AND NOT has_database_privilege(rolname,current_database(),'CREATE,TEMP'))
    """).scalar_one():
        raise CommandError("Provision a separate unprivileged rs_worker login before upgrade.")
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    raise CommandError(
        "Forward only. Restore a verified database and its independent object archive."
    )
