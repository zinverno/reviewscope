"""Expose scoped quality context from a verified integrity artifact."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0009_overview"
down_revision = "0008_review_evidence"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
