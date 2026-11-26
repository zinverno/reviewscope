"""Save versioned manual monitors and order import authority before source locks."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0021_saved_monitors"
down_revision = "0020_topic_lock_order"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
