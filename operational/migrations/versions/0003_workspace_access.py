"""Validate legacy identities before enabling runtime access."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0003_workspace_access"
down_revision = "0002_principals"


def upgrade():
    conn = op.get_bind()
    conn.exec_driver_sql("LOCK TABLE workspace_membership IN SHARE ROW EXCLUSIVE MODE")
    if conn.exec_driver_sql("""
        SELECT EXISTS (SELECT FROM workspace_membership m LEFT JOIN principal p
                       ON p.id=m.principal_id WHERE p.id IS NULL)
    """).scalar_one():
        raise CommandError(
            "Unmapped membership. Apply 0002_principals separately, verify each "
            "legacy identity with its owner, then retry. See access-operations.md."
        )
    if not conn.exec_driver_sql("""
        SELECT EXISTS (SELECT FROM pg_roles r WHERE rolname='rs_runtime' AND rolcanlogin
            AND rolname <> current_user
            AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)
            AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid)
            AND NOT EXISTS (SELECT FROM pg_class WHERE relowner=r.oid)
            AND NOT EXISTS (SELECT FROM pg_database WHERE datdba=r.oid)
            AND NOT EXISTS (SELECT FROM pg_namespace WHERE nspowner=r.oid))
    """).scalar_one():
        raise CommandError("Provision a separate unprivileged rs_runtime login before upgrade.")
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    raise CommandError("Forward only. Restore a verified pre-upgrade backup into a new database.")
