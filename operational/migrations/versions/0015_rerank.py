"""Bind rerank jobs to their immutable candidate parent."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0015_rerank"
down_revision = "0014_aspects"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
