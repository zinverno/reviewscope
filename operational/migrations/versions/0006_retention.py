"""Add the local retention contract without rewriting historical rows."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0006_retention"
down_revision = "0005_background_jobs"


def upgrade():
    if (
        not op.get_bind()
        .exec_driver_sql("""
        SELECT EXISTS (SELECT FROM pg_roles r WHERE rolname='rs_retention' AND rolcanlogin
          AND rolname<>current_user AND NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
          AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid)
          AND NOT EXISTS (SELECT FROM pg_class WHERE relowner=r.oid)
          AND NOT has_schema_privilege(rolname,'public','CREATE')
          AND NOT has_database_privilege(rolname,current_database(),'CREATE,TEMP'))
    """)
        .scalar_one()
    ):
        raise CommandError("Provision the restricted rs_retention login before upgrading.")
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"),
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise CommandError(
        "Forward only. Restore independent objects and replay the latest deletion ledger."
    )
