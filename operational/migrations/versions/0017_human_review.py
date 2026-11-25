"""Record and publish versioned human reviews of a sealed decision run."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0017_human_review"
down_revision = "0016_aspect_decisions"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore an independent copy with current access and deletion authority.")
