"""Freeze project aspect definitions and bind optional candidate searches to exact versions."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0014_aspects"
down_revision = "0013_reviewer_context_plan"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
