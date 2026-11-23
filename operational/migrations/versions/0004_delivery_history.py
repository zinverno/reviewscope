"""Bind historical identities before accepting immutable deliveries."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0004_delivery_history"
down_revision = "0003_workspace_access"


def upgrade():
    conn = op.get_bind()
    conn.exec_driver_sql(
        "LOCK TABLE review_identity,review_version,import_batch IN ACCESS EXCLUSIVE MODE"
    )
    if conn.exec_driver_sql("""
        SELECT EXISTS (
            SELECT i.id FROM review_identity i LEFT JOIN review_version v
              ON (i.workspace_id,i.source_id,i.id)=(v.workspace_id,v.source_id,v.identity_id)
            GROUP BY i.workspace_id,i.source_id,i.id
            HAVING count(DISTINCT v.external_place_id) <> 1 OR bool_or(v.external_place_id IS NULL))
    """).scalar_one():
        raise CommandError("LEGACY_PLACE_UNRESOLVED. Review the scoped mapping before upgrading.")
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    raise CommandError("Forward only. Restore a verified database and its object archive.")
