"""Publish scoped direct review relations in one bounded transaction."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0018_typed_relations"
down_revision = "0017_human_review"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
